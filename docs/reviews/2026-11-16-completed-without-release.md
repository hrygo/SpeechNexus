# 已完成执行的取消操作员出口：非作者审查（PR #906 / #852）

审查对象：`fix/completed-without-release`，head `c523d925`（父 `54507e0b`），PR #906。

> **状态**：本报告第一轮针对 `c523d925`。返修后的 head `930f8930` 已做 delta 复核，见文末
> 「返修复核（head `930f8930`）」——**在 `930f8930` 上：代码侧已无阻塞项，合并前只需修三处文档错误**。

本切片给「执行已 `succeeded`、租约仍 `reserved`、取消无法收口」这一类受理新增一条操作员命令
`python -m speechnexus_backend.completed_release_cli`：用一份钉在 `instance_id` +
`instance_generation` 上的网关 drain 应答作为证词，释放该租约的容量并写一条追加账本行；Job 仍由既有
`UnconfirmedCancellationSettlement` 在下次领取时落 `failed/cancellation_unconfirmed`。

审查在独立 worktree `/private/tmp/sn912` 完成，基线 `origin/main`（`e2b4fc7e`，根 `54f7778e`）。
本分支的合并基是 `54507e0b`，`origin/main` 此后前进 6 个提交，因此**按任务要求用两点口径**
（`git diff origin/main HEAD`）会看到 91 文件；本切片真正新增的改动用 `git diff 54507e0b c523d925`
即 13 文件 +2815/−55。`git merge-tree --write-tree origin/main c523d925` 无冲突。

隔离环境：PostgreSQL 16.16 `127.0.0.1:55480/sn892`、MySQL `127.0.0.1:55434`；`UV_NO_CONFIG=1 uv sync
--locked --all-packages --all-groups`。未提交 `uv.lock`，未改动 `contracts/**`、迁移或部署文件。

**结论（针对 `c523d925`）：不能合。** 释放事务本身是干净的（摘要确实从加锁事实重算、世代钉死、未 drain 拒绝、重放幂等、
executing 租约拒绝，见 §2），`#852` 的「全部租约已落地」不变量也没有被绕开（§3）。但有三条必须修：
**(M1)** drain 应答的 `updated_at` 没有任何新鲜度/时序校验，一份**过期或手写**的 drain 文件即可通过并
释放（实测，§2.1）；**(M2)** 子类标签 `completed-without-release` 只读两个状态，与作者自述的「三条件」
不一致，会把「`succeeded` 但没有 `engine_terminal_response`」——文档明确划给 #338 的那一类——路由到
一个必然拒绝它的出口（§4.1）；**(M3)** 运行手册第 2 步的 `curl` 缺 drain 端点必填的 5 个请求字段，
按文档照做会得到 422，处置序列不可执行（§7.1）。三条都是小改，修完可以合。

---

## 0. 复核记录

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 新增单测 | `uv run pytest tests/unit/backend/test_completed_release_domain.py tests/unit/backend/test_completed_release_cli.py tests/unit/backend/test_readiness.py -q` | 74 passed |
| 新增隔离 PG 集成 | `SPEECHNEXUS_TEST_DATABASE_URL=… uv run pytest tests/integration/test_completed_release_pg.py -q` | 11 passed |
| 既有释放/取消失败套件（未放宽回归） | `… pytest tests/integration/test_quarantine_release.py tests/integration/test_cancel_failure_slot_pg.py -q` | 66 passed |
| 既有隔离释放单测 | `… pytest tests/unit/backend/test_quarantine_release_domain.py tests/unit/backend/test_quarantine_release_http.py -q` | 60 passed |
| 静态检查 | `uv run ruff check …` / `uv run ruff format --check …` | All checks passed / 6 files already formatted |
| 类型检查 | `uv run mypy` | Success: no issues found in 1512 source files |
| 边界检查 | `uv run lint-imports` | Contracts: 9 kept, 0 broken |
| 反例一：去掉世代钉死 | 见 §5 | 3 failed |
| 反例二：允许未 drain 释放 | 见 §5 | 10 failed |
| 篡改还原 | `git status --porcelain` | 空 |

---

## 1. 作者自述逐条核对

| 自述 | 判定 | 依据 |
| --- | --- | --- |
| 三条件识别判据（围栏受理 + 账本未 settled + Job `cancelling`；租约 `reserved`；唯一 intent `succeeded/complete` + `engine_terminal_response` 停止证据） | **部分不成立** | 前两条只用于探针发现与运行手册第 1 步；CLI 与写事务只校验第 2、3 条，**从不读围栏、取消账本与 `speech_jobs`**。实测在**没有任何取消围栏**的 Job 上照样释放成功（§2.7）。第 3 条只校验了「有理由认为」的形态：`require_completed_releasable` 确实要求 `engine_terminal_response` + 两个 flag（`domain/completed_release.py:295`），但**子类标签不校验**（§4.1）。 |
| 子类标签 `completed-without-release` / `execution-unknown` + 类计数 | 成立，但计数口径混用 | `backend/src/speechnexus_backend/speech/infrastructure/readiness.py:243` 只看 `lease_state` 与 `execution_state`；计数按**租约行**累加，而句首 `N accepted cancellation(s)` 按**Job** 计数，多租约 Job 下两数不自洽（实测 §4.2）。 |
| 摘要 `completed-release-evidence/v1`（RFC 8785 + sha256）覆盖 `lease_id`、`submit_intent_id`、`instance_id`、`instance_generation`、整个 drain 应答、`observed_at_us`、`reason`；写事务从锁定事实重算，不符即拒 | **成立** | `domain/completed_release.py:197`；`infrastructure/quarantine_release_sql.py:490-505` 在 `FOR UPDATE` 之后用锁定事实重算 `expected` 并与 `evidence.evidence_digest` 比对；伪造 reason 实测 `invalid`（§2.4）。drain 本身是调用方输入，不是锁定事实（§2.1）。 |
| drain 值作为「实例已终止」证据写进账本行（`evidence_kind`/`instance_id`/`instance_generation`/`evidence_observed_at_ms`/`reason`） | **表述不准确** | 账本行写的是**租约**的 `instance_id`/`instance_generation`（与 drain 钉死后必然相等）、`evidence_kind=operator_verified_instance_terminated`、`observed_at`、`reason`；drain 的 `drain_state`/三个计数/`safe_to_stop_instance`/`updated_at` **不落库**，只存在于 sha256 摘要原文里，事后无法从账本恢复「当时声称的 drain 是什么」。 |
| 释放要求 drain 世代与租约一致、`drained`、`safe_to_stop_instance`、未完成计数为 0 | 成立 | `domain/completed_release.py:265-276`、`DrainRecord.drained`（`:100`）；6 组反例参数化测试各自拒绝（§2.3）。 |
| 走既有 `QuarantineReleaseService`；不改状态机/触发器/`require_releasable`/路由/`contracts/**`；CLI 不写 `speech_jobs` | 成立 | `speech/domain/quarantine_release.py` 零改动（`git diff` 为空）；无迁移、无路由、`contracts/**` 无 diff；CLI 只依赖 `PostgresQuarantineReleases`。 |
| 实测 `held_slots` 1→0、lease `reserved`→`released`、`held_attempts` 1→0 | 成立 | 集成测试与本次复跑一致；账本、配额、attempt 状态同时落地。 |
| 反例：去标签 2 failed；去世代钉死 2+1 failed；允许未 drain 9+1 failed | 基本成立 | 后两条本次重做得到 3 failed 与 10 failed（§5；作者按「2 单测 + 1 集成」「9 单测 + 1 集成」表述，与我按文件汇总的口径一致）。「去标签」我按 `debt_subclass` 的直接单测复核（§4.1），未复跑作者那两组改造。 |

---

## 2. 滥用攻击（按风险排序，全部实测）

攻击面是**操作员命令 + 文件输入**。执行身份是专用释放角色：`operator_verified_instance_terminated`
不是新证据类型（`quarantine_release_schema.py` 的 `CHECK` 常量未变），所以不需要 DDL，也不需要新授权。

### 2.1 伪造 / 过期 drain —— **通过，这是本切片最实质的缺口**

drain 应答是操作员用 `--drain-response <file>` 传进来的 JSON；CLI 只做**结构**校验
（`completed_release_cli.py:151-207`：8 个字段、类型、`updated_at` 是 RFC 3339），不核对来源，也不与
执行自身的时间线比对。于是：

* **手写**一份 `drain_state=drained`、三个计数 0、`safe_to_stop_instance=true` 的文件，摘要由 CLI 自己
  算出来必然匹配——摘要只证明「证词与它覆盖的字段一致」，不证明「网关真的这样回答过」。
* **过期**（stale）drain 同样通过：把 `updated_at` 设成本租约执行**派发之前**的时刻，实测仍然释放：

```
drain_updated_at: 1970-01-01T00:00:00.001000Z     # 1ms，早于 dispatched_at=2s / terminated_at=5s
release: outcome: committed
-> exit=0  held_slots=1→0  lease=reserved→released  ledger=1
```

代码层面能确认没有任何时序校验：`drain.updated_at_us` 只在 `DrainRecord.__post_init__` 里被要求
`>= 1`、在摘要里被覆盖、在输出里被打印；`require_observed_at`（`domain/completed_release.py:279`）
只约束**证词** `observed_at_us` 落在 `[max(recorded_at, dispatched_at, terminated_at), now]`，
不约束 drain 自身的 `updated_at`，也不要求 `drain.updated_at_us <= observed_at_us`。

判据：**发布容量本身不受影响**。`release()` 按本租约的 `reserved_slots` 扣减（`domain/lease_release.py`
的 `held.subtract(lease.reserved)`），所以即使该代次上还有别的活，本租约归还的也是它自己的那份，不会
制造「容量谎报」。真正的风险是：账本永久记下一条**不真实的「该代次已 drain」证词**，而运行手册第 2 步
之后的操作员/流程若据此判定「实例可以停」，就可能停掉仍有活的实例代次。可效验、可低成本修（M1）：
要求 `drain.updated_at_us >= intent.terminated_at_us` 且 `drain.updated_at_us <= observed_at_us`，
并在文档里写明「drain 必须现场取、不得复用旧文件；摘要不是网关签名」。

### 2.2 世代不符 —— 拒绝（与作者一致）

`instance_generation=GENERATION+1` 的 drain 实测 `exit=2`、`completed_release_drain_mismatch`、
`held_slots=1`、账本 0 行。`_require_pinned`（`domain/completed_release.py:265`）同时钉
`instance_id` 与 `instance_generation`，摘要也把两值纳入原文，所以换代次既过不了门，也过不了摘要比对
（另有 `test_a_digest_for_another_generation_cannot_be_replayed_onto_this_lease` 覆盖）。

### 2.3 `drained=false` / `safe_to_stop_instance=false` / 未完成计数非 0 —— 逐项拒绝

`DrainRecord.drained`（`:100`）要求 `drain_state=='drained'` **且** `safe_to_stop_instance` **且**
三个计数之和为 0；`drain_state` 只允许契约里的三值。参数化单测逐项覆盖
`draining`、`quarantined`、`safe=false`、`active/submitting/quarantined` 各 1，全部
`completed_release_not_drained`。集成层用真实事务复测了 `drained+active=1`：`exit=2`、`held_slots=1`、
账本 0 行。

### 2.4 摘要重算是否复用调用方传值 —— 不复用，确为锁定事实重算

`completed_release_in_transaction`（`infrastructure/quarantine_release_sql.py:449`）先
`_lock_release_target` 锁定 pool/quota/host/instance/attempt/lease，再 `_completed_facts` 用
`FOR UPDATE` 锁住 intent 与停止证据，然后把锁定后的 `facts` 交给摘要函数重算：
`expected = completed_evidence_digest(lease_id=facts.lease_id, submit_intent_id=intent.submit_intent_id,
instance_id=facts.instance_id, instance_generation=facts.instance_generation, drain=drain,
observed_at_us=evidence.observed_at_us, reason=evidence.reason)`，`expected != evidence.evidence_digest`
即 `invalid`。调用方无法把「另一个 intent / 另一个代次」的摘要挪用到本租约；实测伪造 reason
（摘要按原 reason 铸、请求带改过的 reason）得到 `invalid`，零写入。
保留意见：`drain`、`observed_at_us`、`reason` 三项按定义来自调用方，其中 `reason` 被重算覆盖，
`observed_at_us` 有上下界，**只有 `drain` 无来源与新鲜度约束**（§2.1）。

### 2.5 同一条证据重放 —— 幂等，不二次释放

第二次执行同一证词：`outcome: replayed`，`leases`/`instances`/`ledger` 三份快照逐字段不变（实测）。
换证词（改 `reason`）再跑：`exit=2`、`quarantine_release_conflict`，零写入。释放后再由**正常路径**释放
（无账本行）的情况由 `_branch_f` 兜底：它要求请求证据与 `execution_stop_evidence` 存储的四字段逐项相等，
而机器证据的 `evidence_kind` 是 `engine_terminal_response`、`reason` 为 NULL，所以操作员证词只会得到冲突，
不会被误当重放。

### 2.6 执行仍在 `executing` 的租约 —— 拒绝

实测 `exit=2`、`quarantine_release_conflict`、`held_slots=1`、账本 0 行。门要求
`intent.execution_state=='succeeded'` **且** `cleanup_state=='complete'`，`executing`/`pending` 都拒绝。

### 2.7 没有围栏的 Job —— 仍然释放（条件 1 不由代码强制）

在一个**完全没有 `speech_job_cancellation_fences` 行**的隔离库里直接跑 CLI：`exit=0`、容量归还、
账本 1 行。这不是写事务的漏洞（容量归还是对的），但它说明运行手册「三条同时成立，缺一不可」的
第 1 条只是操作员自查，不是工具前置。文档要么把第 1 条降级为「先确认这是已受理的取消」，要么在
CLI 里加只读断言（需要给释放身份读围栏/取消账本的授权，属扩权，我不建议在本切片做）。

---

## 3. `#852` 不变量与「公开面说谎」

### 3.1 不变量仍成立

`#852` 的不变量是「`failed/cancellation_unconfirmed` ⇒ 每条租约都已落地」。`isolate_sync`
（`infrastructure/job_dispatch_postgres.py:619-621`）把 `released` 直接计为已落地
（`continue  # Capacity is already back; nothing to isolate.`），`LeaseIsolation.complete` 只看
`gaps`，所以 CLI 释放后 worker 下一次领取即写终态。实测：

```
before CLI          : job=cancelling  cleanup_state=pending   held_slots=1  lease=reserved
after CLI, pre-worker: job=cancelling  cleanup_state=pending   held_slots=0  lease=released
after worker        : job=failed error=cancellation_unconfirmed  cleanup_state=quarantined  held_slots=0
```

`settle` 返回 `settled=True, outcome=exhausted`，不变量没有被绕开。

### 3.2 作者对读面时点的自述是错的

作者自述「调用 CLI 之后、worker 下一次领取之前，`cleanup_state` 仍报 `quarantined`」。实测这段时间报的
是 `cancelling` / **`pending`**；`quarantined` 只在 worker 落终态**之后**出现。两者含义不同：前者是
「取消还没判定完」（保守，安全方向），后者是「判定为未确认 + 租约已落地」。

### 3.3 判定：可接受的过渡，不是新的 `#852` 谎言，但必须写清

`cleanup_state=quarantined` 只在 `failed/cancellation_unconfirmed` 下出现（`domain/public_jobs.py:305`），
而架构文档对它的定义本来就是**落地租约的汇总，不是租约桶的实时求和**
（`docs/architecture/runtime-governance.md:163`：「Job 的全部租约都进入隔离桶（**或已释放**）之后，才写
`failed/cancellation_unconfirmed`」）。本切片用的正是那条「或已释放」分支，所以它不是对公开契约的新违反，
也不会让任何容量数字变假（容量看 `held_slots`/`quarantined_slots`，实测均为 0）。

判据（满足即可留在本切片，否则必须在本片修）：

1. 不变量成立（是，§3.1）；
2. 该值的语义在架构文档中已是「落地汇总」（是，且本切片在 `production-jobs.md` 同步解释了）；
3. 没有自动化消费者把 `cleanup_state=='quarantined'` 当成「容量仍被隔离」去做容量加总或告警升级
   ——本仓库的探针与告警读的是 `cancellation_debt` 明细和槽位数字，不是该枚举；`quarantined` 的方向是
   **少报可用容量**（保守），不会超发；
4. 需要精确判定时能从账本恢复「这条 Job 的哪条租约是被操作员释放的」。

第 4 条是本切片的弱项：两个出口写**同一个** `evidence_kind`，账本无法凭字段区分「已完成释放」与
「未知执行释放」，只能靠摘要重算猜（§1 已记）。建议随 M1 一并把 drain 的原文或独立摘要列落账本
（需要一次 DDL，可另开 issue），或在运行手册里给出按摘要口径复算的核对命令。**它是记录可审计性，
不是容量正确性，我不把它算阻塞。**

---

## 4. 识别判据的完备性与误判

### 4.1 `succeeded` 但没有 `engine_terminal_response` —— **没有留在 `execution-unknown`，实测被误标**

`debt_subclass`（`backend/src/speechnexus_backend/speech/infrastructure/readiness.py:243`）只判 `lease_state=='reserved' and execution_state=='succeeded'`，
不读 `cleanup_state`、不读 `execution_stop_evidence`。所以：

* `succeeded` + **无**终止证据（文档明确划给 #338 的形态）→ 标签 `completed-without-release`，
  而 CLI 对同一行拒绝：`completed_release_requires_terminal_evidence`（既有集成测试就是这个用例）；
* `succeeded` + `cleanup_state='pending'`（差最后一个清理步）→ 标签 `completed-without-release`，
  CLI 拒绝：`quarantine_release_conflict`（本次实测 `exit=2`，`held_slots=1`）。

也就是说，作者自述的第 3 条判据（网关终止证据）**没有进入标签**，而标签的唯一用途就是路由
（文档原话：「报告必须让人一眼看出该走哪条路」）。这会把操作员导向一个必然拒绝的出口，属于
本切片自身交付物的缺陷（M2）。

### 4.2 多租约 Job：标签对得上，计数对不上

同一 Job 两条租约（一条 completed、一条 unknown）+ 另两个 Job，实测：

```
cancellation_debt: 3 accepted cancellation(s) still hold capacity
(completed-without-release=2, execution-unknown=2): job-a -> lease-1(…, subclass=completed-without-release),
lease-2(…, subclass=execution-unknown); job-b -> …; job-c -> …
```

句首 3 = Job 数，括号里 4 = 租约行数。单租约 Job 下两者相等，多租约（`required_stage_ids` 多阶段）
就自相矛盾。文档只写了「前缀后面紧跟的计数是分类」，没有写单位。建议统一按租约计数，或把句首改成
「N Job / M lease」（S1）。

### 4.3 把「正在正常收口」的取消误标 —— 可能，且误标的处置比误放更重

探针的取数条件是「Job `cancelling` + 围栏未 settled + 租约不 `released`/`quarantined`」
（`readiness.py` 的 `_DEBT`）。这个形状在两个时点会短暂成立：执行刚到达 `succeeded`、释放还没落地的那段
窗口。**自动路径确实存在**：`TtsSettlementWorker`（`application/tts_settlement.py:110-125`）对网关返回终态
` succeeded` 的目标会调用同一个 `LeaseService.release` 并 `mark_settled`，`due()` 会一直重试未 settled 的
根。因此只要该 worker 在看，本形状会自行消失；#852 的僵局是**该 worker 拿不到终态读数**（或没在跑）时
才成立。

危害不对称：误放（对一个本会自动收口的租约执行 CLI）是幂等的，一方赢、另一方重放/冲突，容量不会错；
但运行手册第 2 步是 **drain**——drain 会停止该实例代次的**新派发**，而契约里没有「解除 drain」的操作。
也就是说，一次假阳性诊断可能让操作员把一台仍在服务其它 Job 的实例代次停掉新派发。手册没有「先复读、
确认行在多个结算周期内没有变化」的前置步骤（M3 的配套，见 §7.2）。

---

## 5. 反例重做

两处改动都只改 `backend/src/speechnexus_backend/speech/domain/completed_release.py`，跑完即从备份还原，
`git status --porcelain` 为空、`git diff` 为空。

**反例一：去掉世代钉死**（`_require_pinned` 只比 `instance_id`）

```
FAILED tests/unit/backend/test_completed_release_domain.py::test_the_testimony_is_pinned_to_the_lease_instance_and_generation
FAILED tests/unit/backend/test_completed_release_cli.py::test_a_drain_of_another_generation_is_refused_before_any_write
FAILED tests/integration/test_completed_release_pg.py::test_a_drain_of_another_generation_is_refused_by_the_real_transaction
3 failed, 70 passed
```

**反例二：允许未 drain 就释放**（`DrainRecord.drained` 恒为 `True`）

```
6 × test_an_undrained_generation_is_never_released[overrides0..5]
3 × test_an_undrained_generation_is_refused_before_any_write[overrides0..2]
1 × test_an_undrained_generation_is_refused_by_the_real_transaction
10 failed, 63 passed
```

两组都同时被**领域单测、CLI 单测、真实 PG 事务**三层拦住，说明反例不是靠单层测试撑起来的。

---

## 6. 既有断言与守卫有没有被放宽

| 守卫 | 结论 |
| --- | --- |
| `quarantine_release.require_releasable` | **未改**：`speech/domain/quarantine_release.py` 在本切片零 diff；`test_the_quarantine_release_still_refuses_the_completed_shape` 证明未知执行规则没有被扩成也接受已完成执行。 |
| `quarantine_release_in_transaction` | 只做了等价抽取：锁定顺序与 `_FROZEN_LEASE_COLUMNS` 逐字段不变，改为 `_lock_release_target` + `_commit(decide)`；既有 60 项单测 + 66 项集成长套件全绿。 |
| `test_cancel_failure_slot_pg.py` | 只**加严**：两处断言追加 `subclass=` 并与 `(execution-unknown=1)` 比对，另加 `"completed-without-release" not in detail`。 |
| 债务明细长度上界 | **放宽了一处**：`tests/unit/backend/test_readiness.py::test_debt_detail_bounds_how_many_jobs_it_names` 从 `len(detail) < 400` 改成 `< 500`。这是新增 `subclass=` 与类计数后的必然结果，仍有界，但把「明细不能长」这道棘轮松了 100 字符，且产品侧没有独立的硬上限。可接受，建议改为断言「固定行数下长度上界 = 基线 + 新增字段预算」而不是整数 500（S4）。 |
| `contracts/**` / 路由 / 迁移 | 零改动（`git diff c523d925 54507e0b -- contracts migrations` 为空）。 |

---

## 7. 文档与运行手册

### 7.1 第 2 步的 drain 命令不可执行（M3）

`docs/components/production-jobs.md:453` 的 `curl -fsS -X POST "$GATEWAY_INTERNAL_URL/internal/v1/instances/
<instance_id>/drain" -o /tmp/drain.json` 没有请求体。契约
（`contracts/internal/execution-control.openapi.yaml:368`、`DrainInstanceRequest` required 列表
`:1406-1412`）要求 `host_node_id`、`pool_id`、`instance_generation`、`fencing_token`、
`max_drain_wait_ms` **五字段全必填**、`additionalProperties: false`，且 `requestBody.required: true`、
`401` 已定义（需要内部鉴权）。照文档执行会得到 422（鉴权缺失则 401），拿不到 drain 应答。
第 1 步的 SQL 也没有 select `host_node_id` / `pool_id` / `fencing_token`，所以操作员**无法**从第 1 步
凑出第 2 步要的请求体。这是处置序列的硬缺陷。

### 7.2 其它文档问题

* 第 1、4 步用的是 `$SPEECHNEXUS_TEST_DATABASE_URL`（隔离测试 DSN），生产手册应指向迁移/运维身份自己的
  DSN；同一段没有说明释放身份与 `SPEECHNEXUS_QUARANTINE_RELEASE_DATABASE_URL` 的预置方式
  （现有说明散在 `deploy/README.md:912`、`docs/operations/compose-minimal.md:1908`，手册未引用）。
* **缺「确认卡住」前置**：没有要求「跨一个 TTS 结算周期复读同一租约，状态不变才 drain」（§4.3 的危害）。
* **缺失败/未知结果的处置**：CLI 的退出码 3（commit 结果未知）只打印「先读租约与账本再决定」，手册没有
  对应步骤；账本表是 append-only（`QUARANTINE_RELEASE_TRIGGER_DDL`），**没有回滚**，恢复方式只有「用
  同一份证词重放」，手册应明确写出来（S2）。
* 手册第 4 步的核对查询只按 `(instance_id, instance_generation)` 关联账本，不按 `lease_id` 过滤；同一代次
  有多条租约时会把别的租约行一起列出，无法单独证明「本条租约」的证词（S3）。
* 作者自述与手册都把 drain 计数说成「写进账本行」，实际只有摘要覆盖（§1、§3.3）。

---

## 8. 必须修清单

| 级别 | 项 | 动作 |
| --- | --- | --- |
| **M1** | drain 无新鲜度/时序约束，过期或手写文件即可释放（§2.1） | 在 `require_completed_releasable`：`intent.terminated_at_us <= drain.updated_at_us <= facts.observed_at_us`；补 3 个单测 + 1 个真实事务用例；文档写明 drain 必须现场取、摘要不是网关签名。 |
| **M2** | 标签不实现第 3 条判据，把 #338 的形态路由到必拒出口（§4.1） | 探针 `_DEBT` 增加 `execution_stop_evidence` 关联，`debt_subclass` 要求 `engine_terminal_response` + `execution_stopped=1` + `resources_released=1`（并能容忍一行缺失）；或至少把文档表格的处置列改成「本出口 + 终止证据前提」。补一条「无终止证据的行不得标 completed」的测试。 |
| **M3** | 运行手册第 2 步不可执行（§7.1） | 第 1 步 select 出 `host_node_id`/`pool_id`/`fencing_token`/`instance_generation`，第 2 步给出完整请求体与鉴权方式；补「复读确认」与「未知结果 → 同证词重放」两步。 |
| S1 | 类计数单位混用（§4.2） | 句首与括号统一按租约计数，或标明 `N Job / M lease`；`_describe_debt` 加多租约用例。 |
| S2 | 账本无法区分两个出口，drain 原文不落库（§3.3） | 至少文档说明摘要复算口径；建议后续加 drain 原文摘要列（DDL 另开 issue）。 |
| S3 | 手册核对查询不按 lease 过滤（§7.2） | 加 `r.lease_id = '<lease_id>'`。 |
| S4 | 明细长度棘轮 400→500（§6） | 改成按字段预算的上界，并给多租约形态留测试。 |
| S5 | `_read_completed_snapshot` 在 LEFT JOIN 出多行时静默取 `rows[0]`（`quarantine_release_postgres.py`） | 行数 ≠ 1 时按 `reconciliation_required`/冲突拒绝，避免预览与事务各看一行。 |

---

## 9. 未验证与限制

* 「假阳性期间被 drain 的实例如何恢复」只按契约判断（没有解除 drain 的入口），未实测；
* `TtsSettlementWorker` 与 CLI 的竞态只做了代码阅读，未构造真实并发（写事务的 CAS 与幂等已在 §2.5 实测）；
* 两处反例是按「领域单测 + CLI 单测 + 真实 PG」复跑，没有复跑作者「去标签」那一组改造；
* 未在真实网关/引擎上验证 drain 应答（本审查如作者一样证明的是释放规则，不是实例真的停过）；
* 前端与 MySQL 用例与本切片无关，未跑（无 TS/契约改动）。

---

## 返修复核（head `930f8930`，2026-11-16 第二轮）

**结论：能合。** 代码侧不再有阻塞项：M1 的「执行结束前取到的 drain」被真实写事务挡住；M2 的三条判据
在与后续写同一事务、租约与 intent 已加锁之后读取，三种拒绝形态全部零写入；M3 的第 1、3、5 步按文档
原样可执行。合并前必须修三处**文档**错误（不改代码）：不存在的「默认 60 s」结算周期、`--observed-at`
的秒截断会假触发 M1 的时间窗、以及三处仍写「九张关系」的部署文档（本 PR 已把声明面改成 12 张）。
另有两处 P2 残余（drain 没有最大年龄；计划文档把「时间窗强制现场取」说重了）和四条 P3。

delta `c523d925..930f8930`：15 文件 +712/−86；合并基仍是 `54507e0b`，`origin/main` 仍是 `e2b4fc7e`。

| 检查 | 命令/对象 | 结果 |
| --- | --- | --- |
| 领域/CLI/探针单测 | `test_completed_release_domain.py` / `test_completed_release_cli.py` / `test_readiness.py` | 39 / 37 / 14 passed |
| 新增隔离 PG 集成 | `test_completed_release_pg.py` | 16 passed（上轮 11） |
| 既有释放与取消失败 | `test_quarantine_release.py` / `test_cancel_failure_slot_pg.py` | 51 / 15 passed |
| 单测合计（五文件） | `test_completed_release_*` + `test_readiness` + `test_quarantine_release_*` | 150 passed |
| 静态检查 | `ruff check` / `ruff format --check` | All checks passed / 8 files already formatted |
| 类型/边界 | `mypy` / `lint-imports` | 1512 files clean / 9 contracts kept |
| 文档门 | `check-doc-refs.py --baseline …` / `check-doc-statements.py` | RESULT PASS（终态 0 问题）/ RESULT OK |
| 反例（三组） | 见 §5 | 5 / 10 / 2 failed；还原后 106 passed、`git status --porcelain` 空 |

### 1. M1 的时间窗：边界正确，兜底更宽，但**上界不设年龄**（P2 残余）

`require_drain_fresh`（`backend/src/speechnexus_backend/speech/domain/completed_release.py:312`）在
写事务里判 `lower <= drain.updated_at_us <= observed_at_us`，`lower` 取
`terminated_at_us`，缺失时退回 `max(recorded_at_us, dispatched_at_us)`
（`_drain_lower_bound`，`:302`）。逐项实测（都是一条独立的真实 PG 事务，直接调写事务、绕过 CLI 预检）：

| 用例 | drain.updated_at | 结果 |
| --- | --- | --- |
| 执行结束前 1µs | terminated−1µs | 拒绝 `completed_release_drain_stale`，零写入 |
| 下边界（闭） | == terminated | **放行** |
| 上边界（闭） | == observed | **放行** |
| 记录之后 1µs | observed+1µs | 拒绝 `drain_stale`，零写入 |
| 无终止时间、派发后 3s（真实结束 5s） | 3s | **放行**——兜底分支确实比终止时间更宽 |
| 无终止时间、派发前 1.5s | 1.5s | 拒绝 |
| **24 小时前取到的 drain**（终止 5s、observed=now） | now−24h | **放行** |

判定：

- 「执行结束前取到的 drain」这一类（旧文件复用、复制粘贴的旧票证）现在被挡住，这是 M1 的真实收益；
  两个边界值放行，与文档 `terminated_at_us <= updated_at <= observed_at` 的闭区间表述一致。
- **但时间窗证明的是先后，不是新鲜**。`observed_at` 由调用方提供（`--observed-at` 必填），上界就等于
  操作员的「现在」，所以一份**执行结束之后任意久**取到的真 drain 都在窗内（实测 24h 前仍释放成功）。
  「窗内但内容陈旧」因此仍可过：一份在途 drain 之后该代次又接了新活，照样通过。
- 手写 drain 仍不可防（摘要不是签名，文档已明说）。M1 挡的是「诚实的操作员没注意时间」和「旧文件
  自证」，不是伪造者——这与计划文档里「『现场取 drain』由时间窗强制」的说法相比说重了，该句要改为
  「时间窗强制先后，现场取仍靠流程」（`docs/plans/2026-11-16-succeeded-then-cancelled.md` 本轮新增段）。
- 兜底分支（无终止时间）比需要的更宽，但在受判定的形状里**不可达**：`engine_terminal_response` 由
  `execution-gateway/src/speechnexus_gateway/infrastructure/stop_sql.py:96` 与 `terminated_at_us` 在
  同一条 UPDATE 里写入，所以「有终止证据」蕴含「有终止时间」。若某天可达，`facts.stop.observed_at_us`
  已经是现成的更好下界。P3。

### 2. M2 的工具自证：在锁之后读，拒绝时零写入

以下行号均按 `930f8930`（审查分支基于 `origin/main`，被审实现不在该树里，行号引用按 head 计）。

顺序（`backend/src/speechnexus_backend/speech/infrastructure/quarantine_release_sql.py:498-530`）：
`_lock_release_target`（:498，租约与提交意图 `FOR UPDATE`）→ 账本重放判定（:499）→ 已释放则
`_branch_f` 重放（:502-505）→ `_completed_facts`（:506）读围栏/账本/Job（:421/:426/:430）→
`require_completed_releasable`（:513）→ 摘要重算与比对 → `release_in_transaction`（:530）。

三种拒绝形态我用**写事务本身**（不是 CLI 的只读预检）各测一次，拒绝时把八张表的快照逐字段比对：

| 形态 | 错误码 | 零写入 |
| --- | --- | --- |
| 无围栏行 | `completed_release_not_accepted_cancellation` | 是 |
| 账本已 settled | `completed_release_already_settled` | 是 |
| Job 已 `failed` | `completed_release_not_cancelling` | 是 |

- **不是「先写后拒」**：三条 `require_*` 都在第一次写（`release_in_transaction`）之前，异常回滚整事务，
  租约、实例槽位、配额、attempt、intent、停止证据、账本、Job 八份快照完全不变。
- **未加锁读是安全的**：围栏与账本都是 `job_id` 主键的 1:1 行且单调（settled 只会 NULL→值）；Job 状态
  的三条写路径里，未确认判定是「先隔离（会先阻塞在本事务持有的租约行锁上）再写终态」，确认判定与
  dispatcher `_finish` 是「先写状态再释放」，后两条理论上能在读之后翻状态——但容量侧由租约行锁 +
  `version` CAS 兜底，释放只会发生一次，抢跑时 Job 的终态由对方路径决定。所以这个检查是**防御性**的，
  不是容量正确性的唯一依赖；注释只说「释放会重新锁租约」是准确的，别把它读成「Job 状态也是锁定的」。
- 幂等没有被新门破坏：已有账本行时在门之前就 `replay` 返回，租约已 `released` 时走 `_branch_f`，所以
  「释放后取消判定才 settled」的重试仍然重放，不会被 `already_settled` 拒掉。
- 新面没有放大既有读数：探针新增的 `LEFT JOIN execution_stop_evidence` 不会乘行——该表对
  `submit_intent_id` 有唯一约束（实测重复插入报 `execution_stop_evidence_submit_intent_id_key`），
  围栏与账本表是 `job_id` 主键；`quarantine_release_postgres.py:130` 的多行拒绝因此只是防御。
  计数单位已分：探针实测 `1 accepted cancellation(s) … over 1 held lease(s) (held leases:
  completed-without-release=1)`，单测另覆盖 2 Job / 4 租约。
- 测试缺口：三种形态里「Job 不再 `cancelling`」只有 fake adapter 单测，没有真实 PG 用例（我补测了写
  事务，能拒）。P3。

### 3. 权限扩展：必要且最小，但部署文档的「九张关系」必须改（D1）

新增面只有 6 列、全部 `SELECT`：`speech_jobs(job_id, state)`、
`speech_job_cancellation_fences(job_id, accepted_at_us)`、
`speech_job_cancellation_reconciliations(job_id, settled_at_us)`
（`backend/src/speechnexus_backend/speech/infrastructure/quarantine_release_schema.py:87-91`）。每列都被
且只被一条判据使用；`UPDATE_COLUMNS`/`FULL_INSERT` 未变，写面与 lock 面都没动。

启动校验实测（生产形状的独立角色 + 真实 ACL）：

| 场景 | 结果 |
| --- | --- |
| 严格按 `quarantine_release_grants` 预置 | 通过 |
| 缺 `speech_jobs.state` | `Quarantine release privileges do not match the declared surface at speech_jobs.state` |
| 多一列 `speech_jobs.model_id` | 点名拒绝 |
| 给 `speech_jobs.state` 加 `UPDATE` | 点名拒绝 |
| 复原 | 通过 |

判定：**这是最小授权的必要扩展，不是「为了自证扩过头」**。三条判据（围栏存在、账本未 settled、Job 仍
`cancelling`）都要读这三张表，只加列级 `SELECT`，启动按列等值强制，多一列同样拒绝；读到的 `job_id`/
`state` 在本产品（无登录/RBAC/多租户）不构成新的敏感面。真正的「扩过头」风险反而在文档侧：

- 声明面从 **9 张关系变成 12 张**，但 `deploy/compose.env.example:295`、`deploy/README.md:914`、
  `docs/operations/compose-minimal.md:733` 与 `:1111` 仍写「九张关系 / nine relations」。本 PR 改了
  `quarantine_release_schema.py`，这四处必须同步（改成 12，或改成「按函数生成、不要手数」）。
- 漏跑会怎样（三种都实测）：API 进程启动被拒并点名列（`backend/src/speechnexus_backend/app.py:378` → `Quarantine release startup
  rejected (… at speech_jobs.job_id)`）；不起 API 只跑 CLI → 退出码 3
  `unavailable: quarantine_release_privilege_missing`；只读探针身份漏跑 → 降级为
  `cancellation_debt: 1 accepted cancellation(s) … (jobs: lease-unread=1) … [lease detail unavailable:
  permission]`，不让 readyz 失败。**谁必须跑**：任何配置了 `SPEECHNEXUS_QUARANTINE_RELEASE_DATABASE_URL`
  或 `SPEECHNEXUS_READINESS_CANCELLATION_DSN` 的环境，在升级到本版本时都要重跑
  `quarantine_release_grants` 与 `cancellation_debt_reader_grants`；手册应加这一句。

### 4. M3 手册：第 1/3/5 步可跑，三处要改（D2、D3、D4）

- **第 1 步**（`docs/components/production-jobs.md:450`）：在真实 schema 上原样执行，返回 drain 需要的
  五项（`host_node_id`/`pool_id`/`fencing_token`/`instance_generation`/`instance_id`）和两侧状态。
  实测 1 行、字段齐全。
- **第 3 步**（`:471-481`）：五项请求字段与契约
  （`contracts/internal/execution-control.openapi.yaml` 的 `DrainInstanceRequest.required`）一致；
  `Authorization: Bearer` + 组件凭据与网关实现一致
  （`execution-gateway/src/speechnexus_gateway/interfaces/http/control_security.py:12`，scheme
  `ComponentBearer`）；`SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL` / `..._COMPONENT_CREDENTIAL` /
  `SPEECHNEXUS_DATABASE_URL` 都是真实存在的配置键。请求体在本仓库没有可离线执行的网关，未实测，
  按契约与路由签名判定可用。
- **第 5 步**（`:490-501`）：原样执行，释放后 `held_slots=0`，账本行按 `lease_id` 精确命中（实测）。
- **D2**：第 2 步「跨一个结算周期（**默认 60 s 起**）」（`:467`）没有出处。两个相关 worker 的真实默认是
  TTS 结算 1 s（`SPEECHNEXUS_TTS_SETTLEMENT_POLL_SECONDS`，范围 0.1–300）与取消对账 15 s
  （`SPEECHNEXUS_JOB_CANCELLATION_RECONCILIATION_INTERVAL_SECONDS`，且默认 `enabled=False`），
  `deploy/` 没有覆盖它们。改为点名变量，或直接写「至少覆盖一个实际启用的结算/对账间隔」。
- **D3**：第 4 步 `--observed-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"`（`:487`）与 M1 的新窗冲突：网关的
  drain `updated_at` 是微秒精度（`execution-gateway/src/speechnexus_gateway/domain/submission.py:564`
  的 `isoformat(timespec="microseconds")`），`date` 截到秒。实测 drain `…06.700000Z` + observed
  `…06Z` → `completed_release_drain_stale`：**按文档脚本化连跑会出现假拒绝**。改成微秒时间戳
  （`date` 在 BSD 上无 `%N`，用 Python 一行），或明确「drain 与 CLI 至少隔 1 秒」。
- **D4（文案）**：「`unavailable` 表示事务没开始，直接重试安全」（`:526`）把退出码 3 的两种 message
  混成一种——CLI 实际区分 `unavailable:`（事务未开始）与 `unknown:`（提交结果未知，要求先读租约与
  账本再重试）；前半句「提交未知时重放同一份证词」与账本行为一致（同摘要重放 `replayed`、不写第二行）。
- 读模型段与本轮实测一致（CLI 后 `pending`、判定落终态后 `quarantined`）；「工具只读 Job 不写 Job、
  两列 `SELECT`」与代码一致。

### 5. 反例重做（改完即还原）

| 反例 | 破坏点 | 结果 |
| --- | --- | --- |
| 去掉时间窗 | `require_drain_fresh` 改 no-op | **5 failed**（领域 2 + CLI 2 + 真实 PG 1） |
| 重新信任调用者 | `_require_accepted_cancellation` 改 no-op | **10 failed**（领域 5 参数 + CLI 3 + 真实 PG 2） |
| 标签退回两状态 | `debt_subclass` 去掉终止证据 | **2 failed**（`test_a_completed_execution_without_the_terminal_fact_is_not_this_subclass` + `test_the_label_does_not_route_the_unproven_shape_to_that_exit`） |

三组都同时被领域、CLI/探针、真实事务中的两层以上拦住；还原后 `test_completed_release_domain.py` 等
106 passed，`git status --porcelain` 为空、`git diff` 为空。

### 6. 既有守卫

- `quarantine_release.require_releasable`：`git diff 54507e0b 930f8930 -- backend/src/speechnexus_backend/speech/domain/quarantine_release.py`
  为空，未改。
- `tests/integration/test_cancel_failure_slot_pg.py`：只把断言换成新句式（`over 1 held lease(s)`、
  `(held leases: execution-unknown=1)`），保留 `completed-without-release not in detail` 与
  `unavailable not in detail` 两条负断言——等价或更严，未放宽。
- 探针明细长度棘轮仍是 `len(detail) < 500`，本轮没有再放宽。接受理由：产品侧真正的上界是探针的
  `limit`（`CancellationDebtProbe(limit=96)`，`_DEBT` 带 `LIMIT`），`<500` 只覆盖 3 行的用例，抓的是
  单条长度回归；替代判据建议写成 `len(detail) <= 固定前缀 + rows × 每行上限`（或把 `limit` 与断言
  一起钉住）。不阻塞。
- `contracts/**`、迁移、`deploy/**`：`git diff 54507e0b 930f8930 -- contracts migrations deploy` 为空。

### 7. 剩余必须修

| 级别 | 项 | 位置 | 动作 |
| --- | --- | --- | --- |
| **D1** | 声明面已 12 张，文档仍写「九张关系」 | `deploy/compose.env.example:295`、`deploy/README.md:914`、`docs/operations/compose-minimal.md:733`/`:1111` | 改 12 或改为「按 grant 函数生成、不要手数」；补一句升级须重跑两个 grant 函数 |
| **D2** | 「默认 60 s 起」无出处 | `docs/components/production-jobs.md:467` | 点名 `SPEECHNEXUS_TTS_SETTLEMENT_POLL_SECONDS`（1 s）/`…CANCELLATION…INTERVAL_SECONDS`（15 s，默认关闭），或删掉数字 |
| **D3** | `--observed-at` 秒截断会假触发 `drain_stale` | `docs/components/production-jobs.md:487` | 用微秒时间戳，或明确「drain 与 CLI 至少隔 1 秒」 |
| P2 | drain 没有最大年龄（实测 24h 前的 drain 仍释放） | `backend/src/speechnexus_backend/speech/domain/completed_release.py:312` | 要么加一个有界常量（如 15 分钟），要么在手册写明「时间窗只证明先后，不证明新鲜」 |
| P2 | 计划文档把「时间窗强制现场取」说重了 | `docs/plans/2026-11-16-succeeded-then-cancelled.md` 本轮新增段 | 改成「强制先后」 |
| P3 | 「Job 不再 `cancelling`」缺真实 PG 用例 | `tests/integration/test_completed_release_pg.py` | 补一条（我实测写事务能拒） |
| P3 | 退出码 3 两种 message 的文案没区分 | `docs/components/production-jobs.md:526` | 分写 `unavailable:` / `unknown:` |
| P3 | 兜底分支应用 `stop.observed_at_us` 而非派发时间 | `backend/src/speechnexus_backend/speech/domain/completed_release.py:302` | 防御性改法，当前形状不可达 |

### 8. 本轮未验证

- 第 3 步的 drain 请求没有对着真实网关跑（本仓库没有可离线启动的执行网关与引擎），按契约与路由签名
  判定；`curl` 的 401/422 分支未实测。
- 「假阳性期间被 drain 的实例如何恢复」仍按契约判断（没有解除 drain 的入口），本轮未扩大。
- 并发抢跑只做了代码顺序与锁/版本的分析，没有构造两个进程的竞态。
- 第 1、5 步的 SQL 是在隔离 schema 上逐字执行（不是生产 DSN），证明的是语句与列存在、结果形状正确。
