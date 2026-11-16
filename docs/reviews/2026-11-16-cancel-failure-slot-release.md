# 取消失败终态判定的非作者审查（PR #888）

审查对象：`fix/cancel-releases-slots`，head `abefdf55`（父 `d63dae62` = `origin/main`），
`git diff origin/main...HEAD --stat` = 14 文件 +1017/−33。审查在独立 worktree
`/private/tmp/sn894` 完成，隔离 PostgreSQL `sn892-pg:55480`（schema/role 每例自建自清）、
MySQL `speechnexus-coordination-mysql:55434`。
报告交付为独立审查 PR #889（`review/888-cancel-slots`），在被审 head `abefdf55` 之上只加本文件：
`git diff abefdf55...HEAD --stat` = 1 file；对 `origin/main` 的三点 diff 等于被审 14 文件 + 本报告。

**结论：不能合。** 核心机制（确认窗口/预算到达 → 先 `LeaseService.quarantine` 再
`advance("failed", cancellation_unconfirmed)`）的方向、账目算术、幂等与 fencing 都经得起实测，
默认部署的自述也如实。不能合的原因是两条**该修法自身没有覆盖、且没有出口**的分支，
它们让 PR 标题「取消失败的 Job 不再永久占用实例槽位」在可到场景里不成立：

1. **多租约 Job 部分隔离后永久残留**。`quarantine_sync` 逐租约 fail-fast（`job_dispatch_postgres.py:582`），
   只要有一条租约被拒（例如某阶段没有 `unknown_quarantined` 事实），后面的租约**根本不会被尝试**；
   `settle` 又用 `contextlib.suppress` 吞掉返回值（`job_cancellation_runtime.py:290`）照样把 Job 判终态，
   账本写 `exhausted`。实测结果：`held_slots=1 / quarantined_slots=1`，另一条租约永远是
   `reserved`，`restart` 不再领取。**这正是 #852 的形状。**
2. **隔离被拒时槽位仍然永久占用，且没有出口**。执行仍是 `executing`/`stopping`（或已 `succeeded`）
   时 `quarantine` 按设计拒绝（`domain/leases.py:166`），Job 照样落 `failed/cancellation_unconfirmed`，
   于是公开面 `cleanup_state` 由 `(state, error_code)` 推出 `quarantined`（`domain/public_jobs.py:305`）——
   而租约其实是 `reserved`、`held_slots` 仍是 1。这条分支**既无自动出口也无人工出口**：
   `ReleaseLease` 需要调用方与停止证据，而派发器已离场、账本已 `exhausted`；
   操作员那条 `quarantine-releases` 要求 `lease_state='quarantined'`（`domain/quarantine_release.py:263`），
   实测会被 `quarantine_release_conflict` 拒。README 里「容量仍不可用但不再记成某执行持有」的承诺在这一支不成立。

其余为 P2/P3（公开面 409 措辞与 `retryable` 自相矛盾、文档一句「隔离按退避重试」与实测相反、
`quarantined` 出口默认未配置且不关联 Job/`/readyz` 等）。逐条见 §7。

> 说明：本报告只审不改。为验证分支行为，我在工作区内临时新增过探针用例，跑完即删，
> 收尾 `git status --porcelain` 为空（§5）。

---

## 0. 复核记录

| 检查 | 命令 / 对象 | 结果 |
| --- | --- | --- |
| 新增隔离 PG 集成 | `pytest tests/integration/test_cancel_failure_slot_pg.py -q` | **3 passed** |
| 新增/改单测 | `pytest tests/unit/backend/test_job_cancellation_{reconciliation,runtime}.py -q` | **39 passed** |
| 被改的既有集成 | `test_job_dispatch_settlement_pg.py` `test_job_cancellation_fence_pg.py` `test_public_job_cancellation_pg.py` | **36 passed** |
| 租约/预算 MySQL 侧 | `test_production_leases.py`（PG+MySQL DSN 同时给） | **44 passed** |
| 契约套件 | `pytest tests/contract -q` | **294 passed, 1 failed**（`test_binding_defaults_producer_contract.py` 的 node fixture 报 `ERR_MODULE_NOT_FOUND`，本机未 `pnpm install`，与本改无关；本改动没碰任何契约文件） |
| 静态检查 | `ruff check` / `ruff format --check` / `mypy`（3 个被改模块） | 全过 |
| 反例重做 | 3 条（§5） | 全部还原，结论与作者自述一致（A 比自述更严重） |
| 幂等/并发探针 | 5 次串行 + 6 线程并发 `settle`（§1） | 账目稳定，不重复释放 |
| 双租约/部分隔离探针 | 两阶段租约（§1、§2） | 全隔离保守恒；部分隔离永久残留 |
| generation 隔离探针 | 同实例 gen1/gen2（§1） | 旧代隔离不动新代账目 |

`UV_NO_CONFIG=1`（本机 uv 镜像配置会误报 lockfile 过期，与仓库无关），未提交 `uv.lock`。

**已核查、确认不是问题**（避免后续误报）：`except A, B:` 这种逗号写法在 3.14 合法、且是
`main` 上多处既有风格（`domain/public_jobs.py:100` 等），不是语法缺陷；`lease_ids_sync` 用
`SET TRANSACTION READ ONLY` 独立读时钟，`observed_at_us` 不会晚于写入事务的库时钟；
`settle` 经 `asyncio.to_thread` 调用，不阻塞事件循环。

---

## 1. 容量守恒与幂等（最细）

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

## 2. 新终态会不会被当成「成功停止」，quarantined 有没有出口

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

## 3. 契约与公开面

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

## 4. 默认部署里是否等于没生效

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

## 5. 反例重做（3 条，全部独立还原）

| # | 改动 | 预期 | 实测 |
| --- | --- | --- | --- |
| A | 把判定分支改回 `record_exhausted(outcome="exhausted")` + `continue` | 1 集成 + 3 单元红 | **2 集成红**（`test_unconfirmed_cancel_is_judged…`、`test_judgment_is_terminal…`）+ **3 单元红**，比作者自述更严重；集成红在 `speech_jobs` 仍 `cancelling`、`engine_instances held_slots=1` |
| B | 删掉 `settle` 里的 `quarantine_sync`（只留 Job 判定） | 集成卡在 `held_slots=1/quarantined_slots=0` | ✅ 一致：`test_unconfirmed_cancel_is_judged…` 红在 `after["engine_instances"] == [held 0/quar 1]`；另有 2 个单测红（顺序断言、幂等补做隔离） |
| C | 让失败的判定也记 `exhausted`（`judged = True`） | `test_failed_judgment_keeps_the_fence_claimable` 红 | ✅ 一致：`(judged.exhausted, judged.unavailable, judged.retried)` 期望 `(0,1,1)` 实得 `(1,0,0)` |

每条改动后 `git checkout -- <path>` 还原，最终 `git status --porcelain` 为空（探针文件已删除）。

---

## 6. 既有行为有没有被放宽

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

## 7. 必须修清单

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

## 8. 附：与作者自述的差异

| 自述 | 复核 |
| --- | --- |
| 三处卡点定位（`_non_actionable` 无条件 pending、`record_exhausted + continue`、`_CLAIM` 摘除 `exhausted`） | 属实，与 `main` 代码一致 |
| 新配置默认 300s、有界 1–3600 | 属实（`job_cancellation_runtime.py:129`-`135`，单测 `test_confirmation_window_is_bounded`） |
| 参考部署无该服务、worker 默认关 | 属实 |
| 「对既有测试只加一行 `unconfirmed=`」 | 仅集成成立；单元侧改了两个既有断言（未放宽，§6） |
| 反例「1 集成 + 3 单元」 | 实测 **2 集成 + 3 单元** |
| 「隔离被拒绝不阻止终态判定」 | 属实，但随之而来的 P1-2 未被承认 |
| 「隔离按退避重试」 | **不属实**（P2-2） |
