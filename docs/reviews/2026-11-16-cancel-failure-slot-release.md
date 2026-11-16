# 取消失败终态判定的非作者审查（PR #888，两轮合并记录）

审查对象：PR #888 `fix/cancel-releases-slots`。两轮 head 分别是 `abefdf55`（第一轮）与
`e4f4e7a3`（第二轮）；PR 最终以 `54507e0b` 合入当前 `origin/main`。审查人不是作者。
范围：租约、预算与终态语义——容量守恒、取消受理的终态判定、隔离出口、公开契约面、
默认部署里的实际生效范围。

**两轮的结论都是「不能合」**，区别在于第一轮挡的是「部分隔离仍写终态 + 谎报 `quarantined`」，
第二轮挡的是「新探针让 `/readyz` 永久 503 + 一类受理永久停在 `cancelling`」。作者逐条返修后
两条主线都已收口：多租约逐条隔离 + `complete` 门前闸、专用只读 DSN + 探针永不失败。
本文件把两轮记录**完整保留**（发现、实测数字、反例、判据），并按合入 head 标注每一项的最终
归宿：`已修/已合`、`已裁决降级`、`已知限制`。

合并口径：`git diff origin/main HEAD`（两点）。当前 `origin/main` 是重写后的 3+ 提交 lineage
（`54507e0b` → `579df7f4` → `8b290445` → 根 `54f7778e` #881），与第一轮被审分支的旧 lineage
（含 `d63dae62` #884）**没有共同祖先**，三点 diff 不可计算——这也是本文件需要在 `main` 之上
重挂的原因。

## 0. 复核记录（两轮）

| 项 | 第一轮 | 第二轮 |
| --- | --- | --- |
| 被审 head | `abefdf55`（父 `d63dae62` = 当时 `origin/main`） | `e4f4e7a3`（`abefdf55` 之后 29 文件 +1613/−346） |
| 审查 worktree | `/private/tmp/sn894`（独立 worktree，不碰主工作区） | 同左 |
| 数据库 | 真实 PG 容器 `sn892-pg`（`127.0.0.1:55480`），schema/role 每例自建自清；MySQL `speechnexus-coordination-mysql:55434` | 同左 |
| 运行方式 | `UV_NO_CONFIG=1 uv sync --locked --all-packages --all-groups`（纯 `--locked` 不装 workspace 成员会导入失败） | 同左 |
| 反例重做 | 3 条，逐项 `git checkout --` 还原 | 3 条，同左 |
| 自建探针 | 6 个（重复/并发判定、双租约部分隔离、generation 隔离、隔离被拒分支等），跑完即删 | 6 个（隔离后崩溃、并发 `settle`、`attempts=64` 饱和、他人终态分支、三种 DSN 权限、迟到取消死锁） |
| 收尾 | `git status --porcelain` 为空 | 同左 |
| 契约门禁 | `tests/contract` 294 passed + 1 node fixture 环境失败 | `contracts-speech-changes`/`contracts-http-changes`/`contracts-compat`/`contracts-route-conformance` 四项 PASS（`contracts-drift`、`contracts-check` 本机缺 node `ajv`，无法执行） |
| 静态检查 | `ruff`、`ruff format --check`、`mypy`（被改模块）全过 | `mypy` Success 1508 files、`lint-imports` 9 kept、`ruff` 全绿、`tests/ci` 465 passed / 1885 subtests |

两轮都已核查、确认**不是**问题的点（避免后续误报）：`except A, B:` 逗号写法在 Python 3.14 合法
且是仓库既有风格；`lease_ids_sync` 用 `SET TRANSACTION READ ONLY` 独立读时钟，`observed_at_us`
不会晚于写入事务的库时钟；`settle` 经 `asyncio.to_thread` 调用，不阻塞事件循环。

.
## 第一部分 第一轮（head `abefdf55`）：部分隔离与谎报读面

> 本节保留第一轮原文的发现与实测数字，只在节首补合入后的状态。

### 1.1 容量守恒与幂等（最细）

### 1.1 账目算术：守恒成立，且只按「一次 attempt」动一次

隔离只做 `held → quarantined` 的搬移，两部分合计不变：

- 槽位：`held_slots - reserved_slots`、`quarantined_slots + reserved_slots`（`domain/leases.py:180`-`181`）；
- 配额：`first = accounting.quota_state == "held"`，只有第一次隔离动一次
  `held_attempts-1 / quarantined_attempts+1`；后续租约走 `first=False` 分支，只要求
  `quarantined_attempts >= 1`，不再动配额（`domain/leases.py:183`-`186`）。
- 之后真正回收走 `release`：`isolated=lease.lease_state=="quarantined"` 时只减
  `quarantined`/`quarantined_attempts`，**不减 held**（`domain/lease_release.py:116`、`162`-`163`），
  `quota_state` 也必须是 `quarantined` 才允许。所以不存在「既退 held 又让 quarantined 归零」的双重释放。

下界有双保险：领域 `integer(value, minimum=0)`（`domain/runtime_intents.py:46`）与库约束
`quarantined_* >= 0`、`held <= max - quarantined`（`coordination_schema.py:27`、`145`）。

实测（隔离 PG，真实 `LeaseService`）：

| 场景 | 前 | 后 |
| --- | --- | --- |
| 单租约（参考窗口形状） | held 1 / quar 0，attempts 1 / 0 | held **0** / quar **1**，held_attempts **0** / quar_attempts **1** |
| 双租约，两条都具 `unknown_quarantined` | held 2 / quar 0，attempts 1 / 0 | held **0** / quar **2**，held_attempts **0** / quar_attempts **1**（配额只动一次，不是两次） |
| 双租约，一条不具备该事实（`stage-zzz` 排序在前） | held 2 / quar 0 | held **1 / quar 1**，未隔离的那条 `lease_state=reserved` |

### 1.2 幂等：同一判定跑两次不会掉 2

`quarantine` 在 `lease_state == "quarantined"` 且 `reason_code` 相同时直接返回 `replayed`，
不产生任何账目变化（`domain/leases.py:165`-`171`）；落库侧对 lease/instance/quota/host/attempt
全部走 `WHERE version = ?` 的条件更新（`leases_sql.py:79`-`240`，`_update` 行数必须为 1）。
`UnconfirmedCancellationSettlement.settle` 在 Job 已是本错误码 `failed` 时只补做隔离、不再 advance
（`job_cancellation_runtime.py:292`-`293`）。

我构造的重复/并发调用（真实 PG）：

1. 判定成功后，再串行 `settle(job)` 5 次 + 直接 `quarantine_sync` 5 次；
2. 再起 6 个线程，各自新开 `PublicJobs`/`PostgresLeaseSettler`，用 `threading.Barrier` 同时 `settle`。

结果两次快照逐字段相等，且等于首次判定后的状态：
`engine_instances = {held_slots: 0, quarantined_slots: 1}`、
`workspace_quotas = {held_attempts: 0, quarantined_attempts: 1}`、
`lease_state=quarantined, version=2`，无异常、无负数、无二次释放。

### 1.3 fencing：隔离不认调用方 fence，但按租约自身 generation 记账

`quarantine_sync` 不接受任何调用方提供的 fence/token，只按 operation 取租约 id，再由
`quarantine_in_transaction` 以租约自己记录的 `(instance_id, instance_generation)` 加锁并校验
（`leases_sql.py:120`-`127`），并对 `frozen_keys`（含 `fencing_token`）做解锁后复核。因此
旧 fence 的判定只能落在旧 generation 的实例账目上。实测：同 `instance_id` 的 gen1/gen2 并存时，
旧租约隔离后 gen1 = `held 0 / quar 1`，gen2 保持 `held 1 / quar 0`，未被跨代改写。

隔离本身也不改写网关执行事实：提交意图行仍是 `unknown_quarantined/quarantined`（新增集成已断言）。

### 1.4 但守恒之外有两个洞

- **fail-fast 循环**：`job_dispatch_postgres.py:582` 的 `for lease_id in lease_ids:` 一旦某条
  返回拒绝就 `return False`，排在后面的租约不再尝试；而调用方 `settle` 用
  `contextlib.suppress(Exception)` 忽略结果并继续判终态，账本随即 `exhausted`。→ §7 **P1-1**。
- **部分提交非原子**：每条租约是独立事务，Job 的终态判定又是另一个事务；中间失败留下
  「一部分已隔离、一部分仍 held、Job 已 terminal」的混合态，而这个混合态没有任何巡检。
  → 与 P1-1 同根。

---

### 1.2 新终态会不会被当成「成功停止」，quarantined 有没有出口

> 事后状态（合入 head `54507e0b` 复核）：P1-1 **已修**；P1-2 的「谎报 `quarantined`」**已彻底消除**，「无出口」由 `cancellation_debt` 可见面 + 文档处置 + #338 承接，其中「执行已终态」这一格降级为**已知限制**（见第四部分 4.2）。
### 2.1 读面与调度逐条核对

| 读面/路径 | 行为 | 判定 |
| --- | --- | --- |
| `engine_instances.held_slots` / `workspace_quotas.held_attempts` | 隔离成功后减一、转 `quarantined_*` | ✅ 正确，容量仍占用 |
| 派发准入 | 准入按 `held + quarantined <= max` 计（领域 `LeaseAccounting` 校验 + 库 CHECK `held_slots <= max_slots - quarantined_slots`），`quarantined` 不计为可用 | ✅ 不会被当成空闲重发 |
| `/v1/nexus/admin/resource-pools` | 单独报 `quarantine_slots`（`resource_pools_postgres.py:40`、`interfaces/http/resource_pools.py:79`） | ✅ 可见，但只是一行计数 |
| `/readyz` | 只跑依赖探针（`speech/application/readiness.py:38`-`60`），不看容量 | ⚠️ 整机槽位全隔离也仍 `ready`，编排面看不到容量损失（P3） |
| 公开 Job `cleanup_state` | 由 `(state, error_code)` 推导，**不看租约**（`domain/public_jobs.py:305`、`interfaces/http/jobs.py:244`） | ❌ 隔离被拒时也报 `quarantined`（§2.2） |
| 公开 Job `retryable` | `failed` 且本错误码 → `False`（`domain/public_jobs.py:467`） | ✅ 不当成功、不自动重试；但见 P2-1 |
| 再次取消 | Job 终态 → `JobCancellationResult("cleanup_pending")`（`public_jobs_postgres.py:824`）→ 409 `execution_cleanup_pending` | ⚠️ 措辞见 P2-1 |
| 下游「已停止可回收」 | 派发/结算路径只在派发器仍持有 claim 时释放（`job_dispatcher.py:483`-`502`）；本场景派发器早已离场 | ✅ 没有把 `failed` 当成功去释放 |

**一个重要事实**：`failed/cancellation_unconfirmed` 在 `origin/main` 上**没有生产者**——
`git grep cancellation_unconfirmed origin/main -- backend/src` 只命中读面（retry 守卫、
`cleanup_state`、取消守卫），从来没被写进过 Job 行。也就是说本 PR 是第一个让这个终态真正可达的改动，
`cleanup_state='quarantined'` 与 `retryable=False` 首次成为线上事实；因此这两个读面的
前提（「租约确实已隔离」）是否成立，正是本 PR 必须负责的部分。

### 2.2 隔离被拒那一支：谎报 + 无出口（P1-2）

执行事实不满足隔离前提时（网关不可达、执行仍 `executing`/`stopping`、或执行已 `succeeded`），
`quarantine` 按设计拒绝，`settle` 仍返回 True 并把 Job 落终态。实测（4 种执行状态各一例）：

```
execution_state=executing/pending      → Job failed/cancellation_unconfirmed
                                         engine_instances held_slots=1 quarantined_slots=0
                                         workspace_quotas held_attempts=1 quarantined_attempts=0
                                         admission_leases  lease_state=reserved cleanup_state=pending
                                         ledger last_outcome=exhausted settled_at_us=NULL
                                         cleanup_state(job)=quarantined  retryable(job)=False
                                         重启再跑：claimed=0（永不再处理）
execution_state=stopping/pending       → 同上
execution_state=succeeded/complete     → 同上（执行已成功，仍被判成「取消未确认」）
execution_state=aborted/pending        → 同上
```

这不是边角：网关不可达时本地 `submit_intents` 恰恰会停在 `executing`/`stopping`，
这也是「取消了停不下来」最常见的形态；作者文档把它写成「保持其持有者记录的状态，需 #338 的停止证据
或操作员处置」（`docs/components/production-jobs.md:295`-`296`），但**没有持有者了**：
派发器早已离场，对账账本 `exhausted` 不再领取（实测 `restart claimed=0`）。

出口核查：

- `ReleaseLease`：需要调用方 + 已验证停止证据（`domain/lease_release.py:116`-`155`），无任何巡检调用它；
- 隔离解除 `POST /v1/nexus/admin/admission-leases/{lease_id}/quarantine-releases`
  （`interfaces/http/quarantine_releases.py:40`）：`require_releasable` 要求
  `lease_state == "quarantined"`（`domain/quarantine_release.py:263`-`268`），而这里租约还是
  `reserved` → `quarantine_release_conflict`；
- 结论：这一支只剩手工 SQL。**与 PR 标题直接冲突**，也是我认为不能合的主因之一。

### 2.3 `quarantined` 有没有出口：有，但是人工的、默认关的、拿不到 id

成功隔离那一支确实有出口，不是「换个桶继续永久占」：

- 入口 `POST /v1/nexus/admin/admission-leases/{lease_id}/quarantine-releases`，
  evidence_kind 固定为 `operator_verified_instance_terminated`，四元组（lease/evidence_digest/
  observed_at/reason）参与 RFC 8785 摘要与幂等键（`domain/quarantine_release.py:261`-`274`、`292`-`312`）；
- 它需要**独立非 owner 身份** `SPEECHNEXUS_QUARANTINE_RELEASE_DATABASE_URL`（`backend/src/speechnexus_backend/settings.py:174`），
  而 `deploy/compose.env.example:304` 默认留空、`deploy/compose.yaml:237` 传空 → 默认部署里该出口不可用；
- 定位信息不足：admin 面只有 `quarantine_slots` 计数，没有任何面把 `lease_id` 关联回 Job，
  操作员得自己有库权限才能查到 lease（P3）。

---

### 1.3 契约与公开面

- **确实暴露给客户端**：公开 Job 体 `error.code = job.error_code`
  （`interfaces/http/jobs.py:264`-`270`）；`JobError.code` 是开放 pattern
  `^[a-z][a-z0-9_]{2,63}$`（`contracts/speech-openapi.yaml:5387`），不是枚举。
- `contracts/speech-openapi.yaml` 全文 **0 次**出现 `cancellation_unconfirmed`；同样
  `job_retry_not_allowed` 也是 0 次 —— 公开契约本来就不枚举 Job 体的终态/响应错误码，
  所以不存在「暴露了但公开词汇里没有」的机器缺口。真正缺的是**门禁覆盖**：
  `tests/contract` 的词汇门禁都作用在 `_ERRORS`（响应码）上
  （`test_public_job_admission_error_vocabulary.py`、`test_render_refusal_vocabulary.py`），
  没有一条覆盖 Job 体的终态 `error.code`，语义漂移不会被门禁抓住（P3）。
- 内部契约未改（`contracts/internal/execution-control.openapi.yaml:532` 已有该值），与作者自述一致；
  `packages/contracts` 的 `execution/coordination/zero_dispatch` 三个 schema 早已列出该值，无需生成物变更。
- **真实缺口在公开措辞**（P2-1）：`job_errors.py:261`-`276` 对 `execution_cleanup_pending`
  固定 `retryable: true` + `Retry-After: 1`，注释理由是「cleanup finishes on its own」；
  `contracts/speech-openapi.yaml:3398` 同样写 "The cleanup finishes on its own, so `retryable` is true"。
  判别路径下这句话不成立（解除隔离要人工证词），而 Job 体给的却是 `retryable: False`。
  客户端会按 1 秒间隔对一个永不自行完成的终态长轮询。

---

### 1.4 默认部署里是否等于没生效

**属实，而且文档如实这么写。** 核验：

- `deploy/compose.yaml` 的服务清单（`postgres`/`minio`/`api`/`gateway`/`binding-disable-worker`/
  `job-dispatch-worker`/`artifact-retention`/`migrate`/`engine-*`/`admin`/`workbench` 等 21 项）
  **没有**取消对账服务；
- `JobCancellationSettings.enabled` 默认 `False`（`job_cancellation_runtime.py:99`-`102`），
  `job_cancellation_worker.py` 默认只打印关闭状态；
- `docs/components/production-jobs.md:370`-`372` 明确写「未开启该 worker 的部署没有取消结论的推进者…
  参考部署 `deploy/compose.yaml` 目前不包含该服务」；`docs/plans/mvp-release-readiness.md:73`-`88`
  把「参考部署未运行该 worker，因此『默认部署里取消能自愈』没有部署级证据」写进「仍未覆盖」。

所以「默认部署里取消能自愈」**不成立**，且文档没有让读者误以为修完就自愈。这一项通过。

---

### 1.5 反例重做（3 条，全部独立还原）

| # | 改动 | 预期 | 实测 |
| --- | --- | --- | --- |
| A | 把判定分支改回 `record_exhausted(outcome="exhausted")` + `continue` | 1 集成 + 3 单元红 | **2 集成红**（`test_unconfirmed_cancel_is_judged…`、`test_judgment_is_terminal…`）+ **3 单元红**，比作者自述更严重；集成红在 `speech_jobs` 仍 `cancelling`、`engine_instances held_slots=1` |
| B | 删掉 `settle` 里的 `quarantine_sync`（只留 Job 判定） | 集成卡在 `held_slots=1/quarantined_slots=0` | ✅ 一致：`test_unconfirmed_cancel_is_judged…` 红在 `after["engine_instances"] == [held 0/quar 1]`；另有 2 个单测红（顺序断言、幂等补做隔离） |
| C | 让失败的判定也记 `exhausted`（`judged = True`） | `test_failed_judgment_keeps_the_fence_claimable` 红 | ✅ 一致：`(judged.exhausted, judged.unavailable, judged.retried)` 期望 `(0,1,1)` 实得 `(1,0,0)` |

每条改动后 `git checkout -- <path>` 还原，最终 `git status --porcelain` 为空（探针文件已删除）。

---

### 1.6 既有行为有没有被放宽

- 集成侧：确实只有追加装配——`test_job_cancellation_fence_pg.py`、`test_public_job_cancellation_pg.py`
  各加 `_NoJudgment` + `unconfirmed=`；`test_job_dispatch_settlement_pg.py` 用真实
  `UnconfirmedCancellationSettlement`（构造必需）。没有删改既有断言。
- 单元侧：**不是只有一行**。`test_attempt_budget_exhausts_without_infinite_retry` 改名为
  `…judges_the_acceptance…`，`test_terminal_unconfirmed_is_exhausted_without_settlement` 改名为
  `…is_judged_and_never_settled`，并把 `last_outcome` 的期望从 `terminal_unconfirmed` 改成
  `exhausted`；同时新增了 `judgment.calls` 断言（没有放宽）。语义副作用见 P3-1。
- 作者「对既有测试只加一行 `unconfirmed=` 装配」的自述只对集成成立，对单元不成立——但改动是
  随行为变化必需的，且没有削弱断言。

---

### 1.7 必须修清单

> 本清单是第一轮当时的状态。逐条的最终归宿见第四部分 4.1 的裁决表：P1 两条、P2 三条均已修；P3 里 3 条已有可见面、2 条仍开放（列入已知限制）。
### P1

1. **多租约隔离必须逐条尝试并留下可重试的失败，而不是整截断**
   （`job_dispatch_postgres.py:566`-`591`；`job_cancellation_runtime.py:290`-`291`）。
   实测已复现：两条租约、一条被拒 → `held_slots=1 / quarantined_slots=1`，另一条永久 `reserved`，
   账本 `exhausted` 后无人再问。至少要做：把「哪些租约隔离失败」作为可重试结果返回（而不是
   `contextlib.suppress` 丢弃），在账本里保留可领取状态直到所有租约都到达 quarantined 或
   **明确**进入人工处置；否则不要在 Job 上写 `cancellation_unconfirmed`（可选择另一码或保持
   可判定的 pending，但需要设计决定）。当前状态是把 #852 搬到多阶段 Job 上。
2. **隔离被拒分支不得谎报 `quarantined`，且必须给出出口**。两条路任选其一并在文档/契约同步：
   (a) 让 `cleanup_state` 依赖租约真实状态（或新增一个「执行未知、容量仍 held」的读面值），
   并把 Job 终态与容量状态解耦；(b) 保留现读面但补齐「该 Job 仍有 held 租约」的运维出口
   （例如允许 `quarantine-releases` 对 `reserved` 租约按 `operator_verified_instance_terminated`
   释放，或在 admin 面把 Job→lease→holding 状态暴露出来）。当前 (2.2) 实测是「无自动出口、
   无人工出口、只剩手工 SQL」。

### P2

1. **公开面的 `retryable`/轮询提示与本终态相反**：`job_errors.py:261`-`276` 给
   `execution_cleanup_pending` 固定 `retryable: true` + `Retry-After: 1`，`speech-openapi.yaml:3398`
   写「cleanup finishes on its own」；判别路径要人工解除隔离，永不自愈，而 Job 体给
   `retryable: False`（`interfaces/http/jobs.py:268`）。需要把「本地证词可等」与「需人工解除」
   分成两个回答（不同 code 或不同 retryable），至少在判别路径上不要建议 1 秒轮询。
2. **文档一句与实测相反**：`docs/components/production-jobs.md:295` 说「隔离按退避重试」。
   实测：只要 `settle` 返回 True（隔离被拒也算 True），账本就写 `exhausted`，重启 `claimed=0`
   → **根本不会重试**。只有「判定本身失败」（`settle` 返回 False）才走 `record_retry` 退避。
   建议改成「判定失败按退避重试；隔离被拒时租约保持原状并进入人工处置（当前无自动出口）」。
3. **`held_slots` 下降但配额与 Job 语义的耦合要在文档里写清**：同一 attempt 的多条租约只动
   一次 `held_attempts`（实测 2 条租约 attempts 仍为 −1），这是对的，但文档只写「减一」，
   容易让运维以为按租约数减。

### P3

1. 账本丢掉了 `terminal_unconfirmed` 与「预算耗尽」的区分：两者都写 `exhausted`
   （`job_cancellation_reconciliation.py:192`-`211`，单元测试同步改期望）。`last_outcome`
   是唯一的事后诊断字段，建议保留原值或新增列，别让「网关说执行已结束」与「worker 放弃」
   事后不可分辨。
2. `ReconciliationBatch.unavailable` 被复用来表示「判定未能落库」（`…:236`-`240`），
   与「网关不可用」混在一个计数里，指标会漂。
3. `quarantined` 全隔离不影响 `/readyz`（只探依赖），容量损失只能从 admin 面计数看出；
   建议至少给一个告警/指标口径，否则「槽位被隔离」在生产上等于静默。
4. `quarantined` 的人工出口默认未配置（`deploy/compose.env.example:304` 为空）且没有任何面
   把 `lease_id` 关联回 Job，操作员实际拿不到入口 id。
5. Job 体终态 `error.code` 没有任何契约门禁覆盖（§3），本 PR 是它的第一个生产者，
   值得补一条词汇/取值门禁或至少在契约里给出说明，避免以后代码与文档各说一套。

---

### 1.8 附：与作者自述的差异

| 自述 | 复核 |
| --- | --- |
| 三处卡点定位（`_non_actionable` 无条件 pending、`record_exhausted + continue`、`_CLAIM` 摘除 `exhausted`） | 属实，与 `main` 代码一致 |
| 新配置默认 300s、有界 1–3600 | 属实（`job_cancellation_runtime.py:129`-`135`，单测 `test_confirmation_window_is_bounded`） |
| 参考部署无该服务、worker 默认关 | 属实 |
| 「对既有测试只加一行 `unconfirmed=`」 | 仅集成成立；单元侧改了两个既有断言（未放宽，§6） |
| 反例「1 集成 + 3 单元」 | 实测 **2 集成 + 3 单元** |
| 「隔离被拒绝不阻止终态判定」 | 属实，但随之而来的 P1-2 未被承认 |
| 「隔离按退避重试」 | **不属实**（P2-2） |


## 第二部分 第二轮（head `e4f4e7a3`）：不变量、探针与审批类

> 本节保留第二轮原文。第一轮已通过的容量算术不再重跑。

第二轮关注作者返修后**新引入**的攻击面：「`failed/cancellation_unconfirmed` ⇒ 每条租约已落地」
不变量、`cancellation_debt` 探针、响应头移除审批类、`LEAST(attempts+1, 64)`。

第二轮结论（原文）：**不能合**。上一轮两个 P1 里，「部分隔离仍写终态」与「谎报 `quarantined`」确实修好了，
逐条隔离、门前闸、读面去谎都经得起实测；但本轮引入了一个**部署级阻断**（新探针在参考部署的
API 身份上权限不足，`/readyz` 永返 503），并且「执行已终态但无停止证据」这一类受理现在**永久
停在 `cancelling`**，与本文件自己写的「必须在有界时间内到达明确的算力结论，不能停在
`cancelling`」直接冲突。两条都需要返修。

### 2.0 复核记录

| 项 | 值 |
| --- | --- |
| 审查 head | `e4f4e7a3`（`9f5d15b8` → `e4f4e7a3` 两笔，相对上一轮 `abefdf55` 29 文件 +1613/−346） |
| worktree / 分支 | `/private/tmp/sn894`，`review/888-cancel-slots-r2` |
| 数据库 | 真实 PG 容器 `sn892-pg`（`127.0.0.1:55480`，schema 每用例新建、用例后 DROP） |
| Python | `UV_NO_CONFIG=1 uv sync --locked --all-packages --all-groups`；3.14 |
| 实测用例 | 我自建探针 6 个（隔离后崩溃、并发 `settle`、`attempts=64` 饱和、另一终态分支、三种 DSN 的探针权限、迟到取消死锁），跑完即删；`git status --porcelain` 收尾为空 |
| 门禁 | `mypy` Success 1508 files；`lint-imports` 9 kept / 0 broken；`ruff` 全绿；`tests/ci` 465 passed / 1884 subtests |
| 基线异常 | `origin/main` 本轮被改写为仅 3 个提交（根 `54f7778e` #881），被审分支基于旧 lineage（含 `d63dae62` #884）；两者**无共同祖先**，`git diff origin/main...HEAD` 不可计算。合并前需要先把 #888 rebase 到当前 `main`，本报告因此以被审 head 为 diff 基线 |
| 契约门禁 | `contracts-speech-changes`、`contracts-http-changes`、`contracts-compat`、`contracts-route-conformance` 四项 PASS；`contracts-drift`、`contracts-check` 本机无法执行（缺 node 依赖 `ajv`，`ERR_MODULE_NOT_FOUND`，与本次改动无关） |

本轮没有重跑上一轮已通过的容量算术（持有/隔离桶的对称性、`quota` 只移动一次、DB 下界与
`WHERE version=?`），那部分结论沿用，不再复述。

### 2.1 不变量攻击：`failed/cancellation_unconfirmed` ⇒ 每条租约已落地

> 事后状态：不变量与三条攻击结论在合入 head 上未变；`job_cancellation_runtime.py`、`job_dispatch_postgres.py`、`job_cancellation_reconciliation.py` 自 `e4f4e7a3` 起无改动。
### 1.1 结构面：唯一生产者 + 门前闸

`cancellation_unconfirmed` 在 head 上仍只有一个生产者：`UnconfirmedCancellationSettlement.settle`
里的 `self._jobs.advance(..., "failed", error_code=self._reason_code)`。它的唯一入口是
`if not isolation.complete: return Judgment(settled=False, ...)`，而
`LeaseIsolation.complete` 定义为 `not gaps`，`gaps` 只在「逐条 `quarantine` 未得到
`lease_state == quarantined`」时追加（`backend/src/speechnexus_backend/speech/application/leases.py:70-83`、
`backend/src/speechnexus_backend/speech/infrastructure/job_dispatch_postgres.py:592-646`）。

租约来源也查了：全仓 `INSERT INTO admission_leases` 只有一处（`backend/src/speechnexus_backend/speech/infrastructure/admission_sql.py:293`，原子准入
事务）。Job 已存在之后不会再出现新租约，因此「读快照时没有、判定时才有」的新租约不可达。
`released` 租约被显式跳过且不计 gap——容量已归还，不是「仍被持有」，分类正确。

结构上我没有找到能让终态提前落地的路径。下面三条是实测过的攻击：

**攻击 1：隔离之后、写终态之前崩溃。**
先直接调 `isolate_sync`（租约全部 `quarantined`，Job 仍 `cancelling`，模拟 kill 在这一刻），
再跑一轮 reconciler：

```
RESUME claimed=1 exhausted=1 retried=0
  job failed/cancellation_unconfirmed status_version=3
  lease quarantined/quarantined version=2
  engine_instances held_slots=0 quarantined_slots=1
  ledger attempts=1 last_outcome=exhausted
restart → claimed=0
```

重启后在「租约已被隔离」的前提下重复判定并补写终态，`quarantine` 的 replay 分支使重复隔离幂等，
没有第二次移动配额。**不变量成立。**

**攻击 2：两条租约同一瞬间一条成功一条失败，失败那条后来才可隔离。**
既有用例 `test_one_lease_without_the_execution_fact_blocks_the_terminal_judgment` 覆盖并实测通过：
第一轮一隔离一阻断 → Job 保持 `cancelling`、`cleanup_state=pending`、账本
`last_outcome=stop_unconfirmed`、`attempts=2`；重启仍 `claimed=1`；补上执行事实后同一 claim 收口。
把逐条尝试改回 fail-fast 会让这一格直接破（见 §4 反例 C：3 个集成用例红），说明逐条尝试不是
可选优化而是不变量的必要条件。**不变量成立。**

**攻击 3：并发判定。** 3 线程用同一 `Barrier` 同时 `settle`（各自独立连接）：

```
[Judgment(settled=True, outcome='exhausted'),
 Judgment(settled=False, outcome='unavailable', detail='job advance unavailable'),
 Judgment(settled=False, outcome='unavailable', detail='job advance unavailable')]
最终 job failed / lease quarantined / held 0 quar 1 / quota 0→1
事后串行再调 → settled=True detail='already cancellation_unconfirmed'，状态不变
```

`advance` 的版本 CAS 保证只有一个写入者落地终态，落败者拿到 `unavailable` 并保持行可领取，下一轮
收敛。**不变量成立。**

### 1.2 `LEAST(attempts+1, 64)`：没有隐形缺陷，但也没有测试

关注点是对的——`attempts` 从「可无限增长的重试计数」变成「封顶 64 的排程计数」后，任何
`attempts > N` 的判断都可能在 64 处静默失效。逐项核对：

- 表约束 `attempts BETWEEN 0 AND 64`（`backend/src/speechnexus_backend/speech/infrastructure/job_cancellation_fence_schema.py:51-52`），
  `_MAX_ATTEMPTS = 64`（`backend/src/speechnexus_backend/speech/infrastructure/job_cancellation_reconciliation_postgres.py:23`），claim 用
  `LEAST(attempts + 1, 64)`；
- 唯一依赖它的判断是 `_judgeable` 的 `candidate.attempts > self._max_attempts`，而
  `max_attempts` 构造函数上限为 32（`1 <= max_attempts <= 32`），因此 `64 > 32` 恒真——
  饱和**不会**让判定静默停止；
- 退避 `_backoff_us` 的指数在 17 处已饱和，与 64 无关。

实测两个方向：

```
阻断态，先跑一轮再置 attempts=64：
  claim=1 retried=1（第 2、3 轮仍被领取）attempts 稳定在 64  last_outcome=stop_unconfirmed
  job 仍 cancelling，无 CheckViolation
可隔离态，attempts 由 1 起正常判定：exhausted=1，attempts 保留被领取时的值
```

也就是说：**不设 `LEAST` 才是 bug**——第 65 次领取会触发 `CHECK (attempts BETWEEN 0 AND 64)`，
claim 事务整体报错，这一行从此永久不可领取（每次领取都报错）。修复方向正确。

但这条新 SQL **没有任何测试覆盖**：`rg "64|LEAST"` 在 `tests/` 里找不到对应断言，集成用例的
`attempts` 最多到 3。按默认退避（上限 300s）跑满 64 轮约 5 小时，真实部署一定会到。建议补一个
「把账本 `attempts` 置 64 后仍能领取且不违反约束」的集成断言。

### 2.2 `cancellation_debt` 探针：判据正确，但它自己会让 `/readyz` 永久 503

> 事后状态：**P1 已修**。合入版改为可选专用只读 DSN（`SPEECHNEXUS_READINESS_CANCELLATION_DSN`）、探针永不失败（读不到只进 detail）、租约/执行表无权限时降级为 Job 级明细、并复用一条连接；新增 `cancellation_debt_reader_grants` 列级最小授权。详见第三部分 3.1。
### 2.1 判据核对（假阳/假阴）

`_DEBT` 的口径：围栏已受理 + `speech_jobs.state='cancelling'` +
账本未 settled（含「无账本行」）+ 受理时间早于 `now - grace`（默认 900s，大于默认确认窗口 300s
加退避），并用 `LEFT JOIN admission_leases ... lease_state NOT IN ('released','quarantined')`
只把仍持有容量的租约算作 blocker。

- **假阳**：`grace 900s > window 300s`，正常在飞行的判定不会被算成债务；账本 `settled_at_us`
  非空即排除；Job 一旦落终态（`cancelled`/`failed`）就不再满足 `state='cancelling'`。我按
  「受理 1000s、租约已 `quarantined`」构造，得到 `... -> no-held-lease` 的明细——这不是假阳，
  而是「判定已完成、Job 还没落地」的真实债务（崩溃窗口正好落在这一格），标签也如实说明没有
  持有容量的租约。**判据本身经得起检验。**
- **假阴（作者声称覆盖的那一格）**：`r.job_id IS NULL` 分支把「worker 从未运行过」算作债务，
  实测确认（其集成用例 `test_the_readiness_detail_names_the_blocking_lease` 先断言无账本行时
  detail 非空，我也复现了同一结果）。
- **盲区**：Job 已是 `failed/cancellation_unconfirmed` 而租约仍 `reserved` 时探针返回 `None`
  （实测：`UPDATE speech_jobs SET state='failed', error_code='cancellation_unconfirmed'` 后
  detail 为空）。这是设计使然（不变量保证该组合不可达），但它意味着探针**无法见证它所守护的
  不变量被打破**，只能见证「阻断在 cancelling」这一格。作为防御纵深有缺口，记为 P3。

### 2.2 部署级阻断（P1）

`_build_readiness` 用 `settings.database_url`（即参考部署的 `SPEECHNEXUS_API_DATABASE_URL`，
`deploy/compose.yaml:145` → `sn_api`）构造探针。参考部署给 `sn_api` 的授权只有
`coordination_runtime_grants` + `asset_runtime_grants` + `speech_synthesis_reader_grants`
（`docs/operations/compose-minimal.md:597-655`），而这条 SQL 还要读
`speech_job_cancellation_fences`、`speech_jobs`、`speech_job_cancellation_reconciliations`。

我按这三组授权建角色实测：

```
GRANT DENIED fence  InsufficientPrivilege permission denied for table speech_job_cancellation_fences
GRANT DENIED jobs   InsufficientPrivilege permission denied for table speech_jobs
GRANT DENIED ledger InsufficientPrivilege permission denied for table speech_job_cancellation_reconciliations
GRANT OK     leases / intents
DEBT QUERY DENIED   InsufficientPrivilege
```

`ReadinessService._inspect_probe` 捕获异常后是 `ProbeStatus.FAILED`，`report.ready` 为假，
`/readyz` 返回 **503**（`backend/src/speechnexus_backend/speech/interfaces/http/health.py:53-58`）——与「探针只报告、不改变聚合状态」
正好相反。后果链条：

1. `deploy/compose.yaml` 里 api 容器 healthcheck 就是 `urlopen('/readyz')`（`deploy/compose.yaml:270`），HTTPError 503
   → 容器永远 `unhealthy`；
2. `admin`（`deploy/compose.yaml:698`）与 `workbench`（`deploy/compose.yaml:722`）都写了 `depends_on: api: condition: service_healthy`
   → 两个容器永远不启动；
3. 与之配套的 `GET /v1/nexus/admin/diagnostics` 复用同一个 `ReadinessService`，也拿不到 detail。

这不是「某个 Job 占着槽位」的偶发红灯，而是**零债务时的必然红灯**。我另外测了两个候选身份：

```
sn_job_writer（provision-job-roles.py 的完整授权）→ DENIED admission_leases
sn_job_dispatch（唯一同时具备 coordination + cancellation_reconciliation + public_job 的角色）→ OK
```

也就是说：今天只有派发角色能跑这条 SQL。可选的修法（任一即可）：

- 让探针用自己的最小身份，并为它补三张表的 `SELECT`（fence、`speech_jobs`、账本），
  再加 `admission_leases`/`submit_intents` 的读取面；或
- 探针改用 `SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL`（为空时不装配）；或
- 把「探针执行失败」降级为 `ProbeStatus.OK` + `detail="cancellation_debt: unavailable"`，
  即把它真正做成不改变聚合状态的观察项——但那时它不是就绪信号，文档要给告警口径兜底。

测试之所以全绿，是因为 `tests/integration/test_cancel_failure_slot_pg.py` 的 `_sandbox` 给的是
**并集角色**（同时授予 `job_runtime`、`public_job`、`cancellation_reconciliation` 与
coordination），比任何生产身份都宽。这类「测试角色 ⊃ 部署角色」的偏差值得在本切片里堵一次：
建议加一条以参考部署 `sn_api` 授权集合为准的探针用例。

### 2.3 响应头移除审批类：形态正确，留了一个不会过期的口子

> 事后状态：审批类本身已合入并通过 `contracts-*` 四项门禁；「条目无过期守卫」**未修**，转为已知限制并给出可执行风险描述（见第四部分 4.3）。
`_APPROVED_RESPONSE_HEADER_REMOVALS` 的键是
`(source, path, method, status, from_version, to_version, header)`，四道闸我逐条攻过：

| 攻击 | 结果 |
| --- | --- |
| 用已登记条目顺带批准**另一个**头 | 失效：`expected` 由键拼出精确 finding，只有同名头被移除才会命中；换个头则 finding 留在 `remaining`，红 |
| 借条目携带无关元数据改动（`content` 换成另一个 schema） | 失效：`_response_metadata` 逐字段比对（仅排除 `description`），实测被拒并追加 mismatch finding |
| 同名头、不同 status / 不同 path / 不同版本对 | 失效：pointer 与版本对都在键里，不匹配直接 `continue` |
| 「基线也没有该头」的 inert 分支被将来滥用 | 不会吃掉别的头：inert 只对**同名同指针**生效，别的头根本走不到这条分支 |
| 条目缺 `issue`/`reason` | 失效：显式追加 problem finding，红 |

真实门禁也跑了：`contracts-http-changes`、`contracts-speech-changes`、`contracts-compat`、
`contracts-route-conformance` 四项 PASS，`speech-openapi.yaml` 的 `JobCancellationConflict`
删掉 `Retry-After` 后由该条目精确吸收，生成物 `speech.generated.ts` 与 `source-inventory.json`
的 digest 同步更新，`job_errors.py` 同步把 `execution_cleanup_pending` 移出 retryable 集合并
删除 `Retry-After`——上一轮 P2 的「409 说 1 秒后重试、Job 体说不可重试」自相矛盾已消除。

残留问题（P3）：**条目没有过期机制**。键里的版本对锁定在 `dev18→dev18`，只要版本没 bump，
它就一直生效；若有人认为「头该回来」把它加回 main、随后又删掉，这次删除会被 #852 的条目静默
批准并按 #852 的理由记账。建议在结构门禁里给这类条目加一个「版本对一旦不再是当前 pair 就
要求删除条目」的守卫。另外，删掉
`tests/contract/test_approved_http_response_header_removals.py` 里
`test_a_removal_without_an_entry_is_still_rejected` 后，其余用例仍然全绿（该断言在文件内没有
等价兜底；真实门禁由 `make contracts-http-changes` 对照 `origin/main` 兜住，所以线上洞不会被
打开，只是回归覆盖会掉一档）。

### 2.4 反例重做（3 条，逐项 `git checkout --` 还原，收尾 `git status --porcelain` 为空）

| 反例 | 我的改动 | 实测红 | 作者自述 |
| --- | --- | --- | --- |
| A 删闸门 | 删掉 `settle` 里 `if not isolation.complete: return Judgment(...)` | 3 集成 + 4 单元 | 3 + 4 ✅ |
| B 受阻断判定改终态 | `_record_blocked_judgment` 直接记 exhausted，且 `run_once` 计为 exhausted | 5 集成 + 7 单元 | 2 + 3（实测更严重） |
| C 隔离退回 fail-fast | `isolate_sync` 首次失败即 `break`，不再尝试其余租约 | 3 集成 + 0 单元 | 3 + 3（单元侧不变，见下） |

反例 C 值得说清：fail-fast 在集成侧红得和闸门缺失一样，因为它把 `gaps` 一起丢掉了、`complete`
变成真，于是「一条租约仍 `reserved`」与 `failed/cancellation_unconfirmed` 同时成立——正是本轮
要防的破口；单元侧 0 红是因为单测用 fake capacity，不经过逐条循环。结论一致：逐条尝试 +
complete 闸是同一件事的两半，缺一即破。

### 2.5 既有断言没有被放宽

- `tests/fake/readiness.py`：`StaticProbe` 的签名与行为未动，只**新增** `DetailProbe`；
  `test_readiness.py` 原有两条用例保留，新用例断言「detail 不影响聚合状态」。
- `tests/contract/test_error_retryable_semantics.py` 与
  `tests/unit/backend/test_public_job_http_surface.py` 是**行为翻转的显式改写**
  （`retryable: true` → `false`、删掉 `Retry-After` 期望），改写理由写进 docstring；
  后者把分支简化为「capacity → 有 `Retry-After`，其余 → 无 `Retry-After`」，
  覆盖等价（`job_outcome_unknown` 仍由 else 分支断言无头），没有削弱。
- `docs/prd.md` 的 `backend/src/speechnexus_backend/app.py:791` → `backend/src/speechnexus_backend/app.py:800` 与 `tests/ci/test_doc_statements.py` 的引用同步
  修改；实测 `backend/src/speechnexus_backend/app.py:800` 就是 `app.include_router(create_job_router(` 的所在行，引用正确。
- `mypy` Success 1508 files、`lint-imports` 9 kept、`ruff` 全绿，作者关于类型面的自述成立。

### 2.6 必须修清单

> 本清单是第二轮当时的状态。最终归宿见第四部分 4.1；未修的 4 条见 4.3。
### P1

1. **`cancellation_debt` 探针让 `/readyz` 在参考部署上永久 503。** 依据见 §2.2：`sn_api`
   缺 fence/`speech_jobs`/账本三张表的 `SELECT`，探针抛异常 → `ProbeStatus.FAILED` → 503 →
   api 容器永远 unhealthy → `admin`、`workbench` 因 `service_healthy` 永不启动。修法三选一
   （最小授权身份、改用派发 DSN、或把探针失败降级为 OK+detail），并补一条以参考部署
   `sn_api` 授权集合为准的用例，避免并集角色继续掩盖它。
2. **「执行已终态但无停止证据」的迟到取消永久停在 `cancelling`。** 实测：执行
   `succeeded`/`aborted`（cleanup 未 complete）时领域拒绝隔离，`settle` 永远是 blocked，
   Job 停在 `cancelling`，租约停在 `reserved`，`held_slots` 不归零，账本 `attempts` 一路涨到
   64，`cancelling→succeeded` 被 DB 触发器拒绝（`backend/src/speechnexus_backend/speech/infrastructure/public_job_submission_schema.py:59` 起），
   所以既不能完成也不能终态，只能等 #338 或人工。这与 `docs/components/production-jobs.md:273`
   那句「受理过的取消必须在有界时间内到达一个明确的算力结论，不能停在 `cancelling`」自相矛盾，
   也和 PR 标题「不再永久占用实例槽位」不符。至少要做其一：让完成方赢下这次竞争
   （允许 `cancelling → succeeded`，取消让位于既有结果），或给这一类一条有界的操作员路径，
   并且把「不能停在 cancelling」这句话改成与事实一致。

### P2

1. **另一原因失败 + 待办围栏时静默隔离。** 实测：Job 先被他人写成
   `failed/internal_error`（DB 允许 `cancelling → failed`），随后 reconciler 仍会隔离其租约并
   记 exhausted：`cleanup_state=not_required`、`retryable=true`、`held_slots=0/quarantined=1`，
   而 `cancellation_debt` 因为 `state<>'cancelling'` 返回 `None`。公开面于是同时说「无需清理、
   可以重试」和「容量被隔离」，重试还会撞 `capacity_exhausted`。建议：`settle` 在
   `job.state == "failed"` 且 `error_code != CANCELLATION_UNCONFIRMED` 时不要顺手隔离，
   或让 `cleanup_state` 从租约事实聚合而不是只看 `(state, error_code)`。
2. **`/readyz` 每次探测都新开一条 PG 连接**（`CancellationDebtProbe.check` 里
   `AsyncConnection.connect`）。修好 P1 之后，这条查询会跟容器 healthcheck 同频执行，
   与控制面连接数叠加；建议复用池或加最小间隔缓存，并把失败与「查询慢」分开计数。

### P3

1. `LEAST(attempts+1, 64)` 无任何测试覆盖（见 §1.2），补一条置 `attempts=64` 的领取断言。
2. 探针看不见它守护的不变量被打破（Job 已 `failed/cancellation_unconfirmed` 而租约仍
   `reserved` 时返回 `None`，实测）。作为「最后一道可见面」建议把判据扩到
   「Job 已终态但仍有未落地租约」这一格。
3. 响应头移除审批条目没有过期/回收守卫（§3）。
4. 并发判定落败方被计为 `unavailable`（`ReconciliationBatch.unavailable` 同时表示「存储不可用」
   与「版本 CAS 落败」），指标上不可区分；建议单独计一类。
5. `docs/components/production-jobs.md` 新增的「就绪 detail 不改变聚合状态」一句，在本轮实现下
   不成立（P1-1 的根因），修好 P1 后再确认这句话可以原样保留。

### 2.7 与作者自述的差异

| 作者自述 | 实测 |
| --- | --- |
| P1-① 逐条隔离、`gaps`、Job 留 `cancelling`、重启可领取、补事实自动收口 | 成立，且 fail-fast 反例证明它是必需条件 |
| P1-② 终态后置，`failed/cancellation_unconfirmed ⇒ 全部落地` | 不变量在本 head 成立；但「执行已终态」这一类被换成了永久 `cancelling`（P1-2），不是修好而是换了形态 |
| 出口：`/readyz` + diagnostics detail + worker 日志 + 钻取 SQL | 形式齐备，但 `/readyz` 在参考部署直接 503（P1-1），detail 拿不到 |
| P2：`execution_cleanup_pending` 移出 retryable、删 `Retry-After`、文档退避措辞、记账口径 | 成立，契约与生成物同步，`contracts-*` 四项 PASS |
| 新增 `LEAST(attempts+1, 64)` | 方向正确且必要，但无测试覆盖 |
| 契约审批类键精确、必须带 issue/reason、只忽略 `description` | 成立，四类攻击均被挡住 |
| 反例 3+3+4 / 3+4 / 2+3 | 我的重做：3+4 ✅、5+7（更严重）、3+0（单元侧因 fake 不经过逐条循环） |

## 第三部分 合入 head 上的复核

### 3.1 本轮 CI 抓到的两件事

这两条比单个缺陷重要，是「测试全绿但部署必红」的成因，写进审查记录。

**一、探针在参考部署的 `sn_api` 身份上会让 `/readyz` 永久 503，而测试全绿是因为
`_sandbox` 给的是并集角色，比任何生产身份都宽。**

第二轮发现时，`_build_readiness` 用 `settings.database_url`（参考部署的
`SPEECHNEXUS_API_DATABASE_URL` → `sn_api`）构造 `CancellationDebtProbe`，而
`sn_api` 的授权集合只有 `coordination_runtime_grants` + `asset_runtime_grants` +
`speech_synthesis_reader_grants`。按该集合建角色实测：

```
fence  DENIED  permission denied for table speech_job_cancellation_fences
jobs   DENIED  permission denied for table speech_jobs
ledger DENIED  permission denied for table speech_job_cancellation_reconciliations
leases OK      intents OK
DEBT QUERY DENIED
```

`ReadinessService._inspect_probe` 把异常记为 `FAILED` → `report.ready` 为假 →
`/readyz` 503；而 api 容器 healthcheck 就是 `urlopen('/readyz')`
（`deploy/compose.yaml:270`），`admin` 与 `workbench` 又都写了
`depends_on: api: condition: service_healthy`（`deploy/compose.yaml:698`、`:722`）。
后果不是「某个 Job 占着槽位」的偶发红灯，而是**零债务时必然红灯**：api 永远 `unhealthy`、
两个下游容器永远不启动。当时换身份实测：`sn_job_writer` 拒 `admission_leases`，
只有 `sn_job_dispatch`（派发 worker 身份）能跑通——即 API 侧根本没有可用身份。

测试为什么没抓到：`tests/integration/test_cancel_failure_slot_pg.py` 的 `_sandbox` 当时
用**并集角色**（同时授予 `job_runtime`、`public_job`、`cancellation_reconciliation` 与
coordination），比 `sn_api`、`sn_job_writer`、`sn_job_dispatch` 任何一个都宽。
这是「测试角色 ⊃ 部署角色」的系统偏差：只要测试身份比生产身份宽，权限类缺陷就天然隐身。

**二、作者随后把 `_sandbox` 改成由 schema owner 播种 fixture。**

之前是「以被测角色写 fixture」——用被测身份去 `INSERT` 围栏行。这恰好掩盖了「身份读不到」
这一层：只要写成功，读者就会以为同一条连接上的 `SELECT` 也没问题。合入版把播种交给 schema
owner，并留了注释：

```python
# Seeded as the schema owner: the acceptance row is fixture state, and the
# identity under test may legitimately be unable to write or read it.
```

同时 `_sandbox` 拆成三种身份模式，把「测试身份」变成显式参数而不是巧合：
`coordination=False`（最小权限：有 Job/取消/账本、无租约与执行表）、
`api_identity=True`（用比参考部署更严的最小面——只给 `speech_synthesis_reader_grants`——
    复现「API 身份读不到取消账本」这一层；参考部署 §6 实际还给 `sn_api` 授了
    `coordination_runtime_grants` 与 `asset_runtime_grants`，同样缺这三张表，见 3.1 实测）、
`debt_reader=True`（文档里那个可选只读身份）。新增用例直接钉住这两件事：
`test_the_reference_api_identity_keeps_readyz_ok_and_says_why`（仅
`speech_synthesis_reader_grants` 的身份下仍 200、detail 为 `unavailable (permission)`）、
`test_a_role_without_the_coordination_grants_still_names_the_job`（降级为 Job 级明细）、
`test_the_debt_probe_never_fails_a_readiness_report`、
`test_the_probe_reuses_its_session_instead_of_connecting_per_poll`、
`test_an_unconfigured_probe_says_so_instead_of_failing`。

结论（可复用的判据）：**凡是依赖最小授权边界的可见面，fixture 必须由 owner 播种，被测身份只读；
并且至少有一条用例的身份授权等于某个真实部署身份**。并集角色适合测流程，不适合测权限。

### 3.2 合入版的实际修法（供后续读者核对）

- 探针改用可选专用只读 DSN：`settings.readiness_cancellation_dsn`
  （`SPEECHNEXUS_READINESS_CANCELLATION_DSN`），未配置时 detail 为
  `cancellation_debt: unavailable (not configured)`，不改聚合状态。
- `check()` 永不抛：连接/权限/超时/relation 缺失分别归为
  `unavailable (connection|permission|relation|timeout|error)`。
- 租约/执行表不可读时降级为 Job 级明细：`job-… -> lease-unread [lease detail unavailable: permission]`。
- 新增 `cancellation_debt_reader_grants` 列级最小授权，配合文档的只读钻取 SQL。
- 探针复用一条连接（`application_name=speechnexus-readiness-debt`），不再每次轮询新建会话
  ——这也是第二轮 P2-② 的修法。
- api 身份不再被要求额外的表授权：`sn_api` 只保留自己的最小面（参考部署的
  `coordination_runtime_grants` + `asset_runtime_grants` + `speech_synthesis_reader_grants`
  里本就没有 fence / 账本 / `speech_jobs`，实测三者皆拒）。

## 第四部分 裁决表与已知限制

### 4.1 两轮清单的最终归宿（按合入 head `54507e0b` 核对）

| 项 | 内容 | 归宿 |
| --- | --- | --- |
| 第一轮 P1-1 | 多租约 fail-fast 部分隔离仍写终态 | **已修**：逐条尝试 + `gaps` + `complete` 门前闸，反例 C（3 集成红）证明它是必要条件 |
| 第一轮 P1-2 | 隔离被拒仍写终态、谎报 `quarantined`、无出口 | **已修（谎报部分）+ 已裁决降级**：终态后置后读面不再说谎；出口 = `cancellation_debt` 明细 + 文档处置 + #338/"succeeded-then-cancelled" 计划 |
| 第一轮 P2-1 | 409 `retryable:true`/`Retry-After:1` 与 Job 体矛盾 | **已修**：`execution_cleanup_pending` 移出 retryable 集合、删头、契约描述重写、审批条目 `#852` |
| 第一轮 P2-2 | 文档「隔离按退避重试」与实测相反 | **已修**：改为「判定不能完成时按退避重试该行」 |
| 第一轮 P2-3 | 记账口径（按租约 vs 按 attempt）易误读 | **已修**：文档用三类计数逐条说清 |
| 第一轮 P3-1 | 账本丢失 `terminal_unconfirmed` 区分 | 已裁决：设计取舍，`exhausted` 表示「已判定」，原因留在 Job 行与租约；文档写明 |
| 第一轮 P3-2 | `unavailable` 与存储故障混计 | **未修** → 已知限制 4.3-④ |
| 第一轮 P3-3 | 全隔离不影响 `/readyz`，容量损失静默 | **已修**：`cancellation_debt` detail + 告警前缀 + worker 日志 + 只读钻取 SQL |
| 第一轮 P3-4 | `quarantined` 出口默认未配置、无 Job→lease 面 | 部分修：可选只读身份给出 Job→lease 明细；默认部署仍需钻取 SQL |
| 第一轮 P3-5 | Job 终态 `error.code` 无契约门禁 | **未修** → 已知限制 4.3-⑤ |
| 第二轮 P1-① | 探针权限使 `/readyz` 永久 503 | **已修**：专用 DSN + 永不失败 + 降级明细 + 连接复用（第三部分 3.1/3.2） |
| 第二轮 P1-② | 执行已终态（`succeeded`/`aborted` 无停止证据）永久停在 `cancelling` | **已裁决降级为文档化已知限制**：`docs/components/production-jobs.md` 新增「已知例外」段落与实测描述，竞争方案进 `docs/plans/2026-11-16-succeeded-then-cancelled.md`；状态机与触发器不变 → 已知限制 4.3-① |
| 第二轮 P2-① | 另一原因失败的 Job 仍被静默隔离 | **未修** → 已知限制 4.3-② |
| 第二轮 P2-② | `/readyz` 每次探测新开会话 | **已修**：连接复用 + `pg_stat_activity` 用例断言 4 次轮询 1 条会话 |
| 第二轮 P3-① | `LEAST(attempts+1,64)` 无测试 | **未修** → 已知限制 4.3-③ |
| 第二轮 P3-② | 探针看不见「Job 已终态但租约未落地」 | **未修** → 已知限制 4.3-⑥ |
| 第二轮 P3-③ | 审批条目无过期守卫 | **未修**（本轮不做）→ 已知限制 4.3-⑦，附可执行风险描述 |
| 第二轮 P3-④ | 并发落败与存储不可用混计 | **未修** → 已知限制 4.3-④ |
| 第二轮 P3-⑤ | 文档「detail 不改变聚合状态」当时不成立 | **已修**：探针永不抛，该句现在为真 |

### 4.2 P1-② 的裁决依据（为什么降级而不是继续挡）

第二轮反对它，是因为「永久 `cancelling`」与 `docs/components/production-jobs.md` 开头那句
「受理过的取消必须在有界时间内到达明确的算力结论，不能停在 `cancelling`」直接冲突，
且 PR 标题承诺「不再永久占用实例槽位」。合入版没有改状态机，但做了三件事，使它可以作为
**已知限制**收口而不是阻塞项：

1. 把文档改成事实：新增「这个目标有一类已知例外，实现确实会停在 `cancelling`」段落，
   逐字写出实测行为（Job 恒 `cancelling`、租约恒 `reserved`、`held_slots` 不归零、
   `attempts` 饱和到 64 后按最大退避继续领取、触发器拒绝 `cancelling → succeeded`）。
2. 明确归属与后续：等 #338 的停止确认协议；竞争方案写在
   `docs/plans/2026-11-16-succeeded-then-cancelled.md`，`本切片不改变状态机或触发器`。
3. 容量不会无声：这一类受理每天都在 `cancellation_debt` 明细里，且文档写了处置入口
   （网关 abort/drain），能拿到的执行事实会把它推到底。

判据：**「永久占用」这个事实本身不是阻塞项，「静默地永久占用」才是。** 一旦事实被写进文档、
被只读面持续暴露、且归属到具体后续工作，它就从不变量缺陷降级为已知限制——剩下的风险是产品
决策（取消与完成的竞争该谁赢），不是本次改动的正确性。

### 4.3 已知限制清单（当前仍开放）

1. **执行已终态的迟到取消永久停在 `cancelling`**（第二轮 P1-②）。实测：Job `cancelling`、
   租约 `reserved`、`held_slots=1`、账本 `attempts` 一路涨到 64 后饱和、`cancelling → succeeded`
   被 DB 触发器拒绝；只有 `cancellation_debt` 明细和文档处置，无自动收口。归属 #338 与
   `docs/plans/2026-11-16-succeeded-then-cancelled.md`。
2. **另一原因失败的 Job 仍会被静默隔离**（第二轮 P2-①）。实测：Job 先被他人写成
   `failed/internal_error`（DB 允许 `cancelling → failed`），随后 reconciler 仍隔离其租约并记
   `exhausted`：`cleanup_state=not_required`、`retryable=true`、`quarantined=1`，
   而 `cancellation_debt` 因 `state <> 'cancelling'` 返回 `None`。公开面于是同时说
   「无需清理、可以重试」和「容量被隔离」。修法二选一：`settle` 在该分支不隔离，
   或让 `cleanup_state` 从租约事实聚合。
3. **`LEAST(attempts+1, 64)` 无测试覆盖**（第二轮 P3-①）。行为我已实测正确（阻断态置
   `attempts=64` 后连续领取两轮仍 `claimed=1`、无 CheckViolation）；但按默认退避跑满 64 轮
   约 5 小时，真实部署一定会到达，建议补一条「置 64 仍可领取」的集成断言，否则将来有人把
   `LEAST` 改回 `attempts + 1` 会以 5 小时后的 CHECK 报错呈现。
4. **并发落败与存储不可用混计**（第二轮 P3-④ / 第一轮 P3-2）。`ReconciliationBatch.unavailable`
   同时表示「存储不可用」与「版本 CAS 落败（他人已写终态）」，指标不可区分。
5. **Job 体终态 `error.code` 无契约门禁**（第一轮 P3-5）。`cancellation_unconfirmed` 是本切片
   第一个生产者，但公开契约从不枚举 Job 体码，`tests/contract` 的词汇门禁也只覆盖响应码。
6. **探针看不见「Job 已终态但租约未落地」**（第二轮 P3-②）。`_DEBT` 与 `_JOB_LEVEL_DEBT`
   都带 `j.state = 'cancelling'`，所以若 4.1 的不变量将来被打破，探针返回 `None`（实测）。
   作为「最后一道可见面」建议扩到「Job 已终态但仍有未 `quarantined`/`released` 租约」。
7. **响应头移除审批条目无过期守卫**（第二轮 P3-③，本轮明确不做）。风险可执行描述见下。

### 4.4 P3-③ 的可执行风险描述（审批条目无过期）

`scripts/dev-env/check-http-contract-changes.py` 的 `_APPROVED_RESPONSE_HEADER_REMOVALS` 条目
只在 `(source, path, method, status, from_version, to_version, header)` 与当前比较完全相等、
**并且基线仍声明该头**时才生效；只要版本对停在 `0.1.0.dev18 → 0.1.0.dev18`，它就永久有效。
因此下面这条路径会让一次**没有经过审批**的头删除被静默吸收：

> 有人把 `Retry-After` 加回 `contracts/speech-openapi.yaml` 的 `409 JobCancellationConflict`
> （或任何触到同一指针的写法），随后在同一个版本对里又把它删掉。这次删除会命中 #852 的条目，
> 门禁不会要求新审批，也不会在 approved 行之外留下痕迹。

发现方式（动版本对或动该响应头时必须做）：

- 先 `rg "APPROVED_RESPONSE_HEADER_REMOVALS" -A 25 scripts/dev-env/check-http-contract-changes.py`
  核对条目是否仍对应当前版本对；
- 跑 `make contracts-http-changes`，approved 行出现 `[#852: …]` 说明这次删除是条目吸收，
  不是本次审批；
- 改动 `packages/contracts/source-inventory.json` 的版本对时，删除对应条目并让门禁重新要求审批。

清理方式：版本对一变（dev18 → 下一个版本）就删掉这条登记；`tests/contract/test_approved_http_response_header_removals.py`
的 `test_the_header_removal_registry_is_frozen` 会因登记变化而变红，提醒同步修改。

## 第五部分 方法论证据：反例 B/C 的原始数字与判据

审查不是「跑一遍作者的反例看是否变红」，而是自己构造反例并记录**红在哪一类断言**上。
两轮的反例数字保留在这里，作为「作者自报的红灯数不能当覆盖证据」的实例。

### 5.1 三轮反例的原始数字

| 反例 | 改动 | 实测红 | 作者自述 |
| --- | --- | --- | --- |
| 第二轮 A 删闸门 | 删掉 `settle` 里 `if not isolation.complete: return Judgment(...)` | **3 集成 + 4 单元** | 3 + 4 ✅ 一致 |
| 第二轮 B 受阻断判定改终态 | `_record_blocked_judgment` 直接记 `exhausted`，且 `run_once` 计为 exhausted | **5 集成 + 7 单元** | 2 + 3（**实测更严重**） |
| 第二轮 C 隔离退回 fail-fast | `isolate_sync` 首次失败即 `break`，不再尝试其余租约 | **3 集成 + 0 单元** | 3 + 3（**单元侧 0 红**） |
| 第一轮 A | 判定分支改回 `record_exhausted + continue` | **2 集成 + 3 单元** | 1 + 3（实测更严重） |
| 第一轮 B | 删掉 `settle` 里的 `quarantine_sync` | 集成红在 `held 0/quar 1` 断言 + 2 单元红 | 一致 |
| 第一轮 C | 失败判定也记 `exhausted` | 1 单元红 | 一致 |

### 5.2 判据：为什么 B 更严重、C 单元侧 0 红

- **B 更严重（5 集成 + 7 单元 vs 自报 2 + 3）**：把阻断判定写成终态，同时打破三类互不重叠的
  断言——`test_one_lease_without_the_execution_fact_blocks_the_terminal_judgment` 一类（Job 必须
  仍 `cancelling`、账本仍可领取）、退避与批次计数一类（`retried`/`exhausted`/`next_check_at_us`）、
  以及「重启从持久排程恢复」一类。作者的自报大概只统计了第一类，所以数字低估。
- **C 单元侧 0 红**：单测用的是 fake capacity，`isolate_sync` 的逐条循环根本不执行，
  fail-fast 只影响真实适配器；因此「逐条尝试」这个正确性只有集成层能抓。
  这条对下一个人有直接价值：**改动落在适配器循环里时，单测全绿不构成证据**。

### 5.3 对作者的判断（明写在记录里）

- 反例 B 的严重程度被低估，说明「红了几条」这种自报不能当覆盖证据，必须自己跑一遍并记录
  红在哪一类断言上；
- 反例 C 说明分层覆盖的边界：适配器行为只有集成测试能抓，作者把单元红计入总数会让人误以为
  单元层也在保护它；
- 两轮合起来，真正抓到根因的都不是作者给的反例，而是审查自己构造的攻击
  （第一轮的双租约部分隔离、第二轮的三种 DSN 权限对照）。建议后续任何「可见面依赖最小授权」
  的改动，默认附带一条「身份授权 = 某个真实部署身份」的用例（见 3.1 的判据）。
