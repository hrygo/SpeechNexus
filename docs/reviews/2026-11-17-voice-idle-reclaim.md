# 按需加载变体空闲回收与停止证据的非作者审查（PR #917）

审查对象：`feat/voice-idle-reclaim-evidence`，head `284652ff`（父 `ad6a2376`，即审查时的 `origin/main` 顶端），
PR #917；两点口径 `git diff origin/main 284652ff --stat` 为 18 文件 +1979/−14。

审查在独立 worktree `/private/tmp/sn919` 完成：分支 `review/917-voice-reclaim` 从 `origin/main`（`ad6a2376`）新建，
`HEAD` 仅为阅读被审查 head 检出过，本报告是该分支唯一改动。隔离环境：PostgreSQL 16
`127.0.0.1:55480/sn892`、MySQL `127.0.0.1:55434`；所有 `uv` 命令带 `UV_NO_CONFIG=1`。未提交 `uv.lock`，
未改契约、迁移、部署文件或 `tests/support/`。

**返修复核（`5032b15b`）见 §10：** M1 已关闭、CI 全绿，但 M4 的超时路径与 M2 的两处表述仍阻塞，
当前结论仍是不能合；§0–§9 保留针对 `284652ff` 的原时点事实。

**结论：不能合（现状）。** 判定表的设计与只读约束是干净的，但 `decide()` 把"已验证释放"排在"仍持
`reserved` 租约"之前，于是"实例已终止且资源已释放、租约仍 `reserved`"这一真实形态被判成
`reclaimed`（§3.1 在真实 PostgreSQL 上复现），与 `runtime-governance` 判定表第 5 行、`reclaimed` 属性
的 docstring、以及该模块自己的 docstring 三处相反；新增的"无停止证据不得投影 `failed`"门禁在同版本
生产路径上不可达，但一旦被触发（旧版本或非契约状态文档）会把 Job 永久挂在 `running`：没有有界
操作员出口，也不出现在任何债务探针里（§4）。而且 head 的 CI 本身是红的（§0、§7.2）：mypy 报
`test_gateway_instance_reclaim_pg.py:95`，两个**既有** PG 集成用例被新门禁判成 `unknown`——PR 补好了
单元 fake 却漏了这两处集成 fake。这既单独阻塞合并，也第一次证明门禁在非契约状态文档上确实会被踩到。

**合并前必须做四件事（都是小改）：** 修 `decide()` 的判定顺序并补用例（M1）、改正文档里的四处不实
陈述（M2）、修绿 CI（M3）、补上门禁形态的可见性与处置（M4）。修完可以合；其余为可接受但需记录，见 §8。

---

## 0. 复核记录

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 新增/受影响单测 | `uv run pytest tests/unit/gateway tests/unit/backend -q` | **5223 passed, 6 subtests passed**（59.6 s） |
| 网关集成三件套（真实 PG + MySQL 分支） | `pytest tests/integration/test_gateway_stop_evidence.py tests/integration/test_gateway_drain_http.py tests/integration/test_gateway_instance_reclaim_pg.py -q` | **71 passed** |
| 类型检查 | `uv run mypy`（7 个改动源文件） | Success: no issues found in 7 source files |
| 格式 / 静态 | `ruff format --check`（11 文件）/ `ruff check`（9 源文件） | 11 files already formatted / All checks passed |
| 边界 | `uv run lint-imports` | Contracts: 9 kept, 0 broken |
| 既有守护未被放宽（但有既有用例被打红） | `git diff origin/main 284652ff -- execution-gateway/src/speechnexus_gateway/infrastructure/stop_sql.py backend/src/speechnexus_backend/speech/infrastructure/{quarantine_release_sql,quarantine_release_postgres,readiness}.py` | 空 diff；`whole_instance_stop_evidence` 未改。两处既有集成用例被新门禁判成 `unknown`，属未同步而非放宽（§7.2） |
| head 的 CI（PR #917） | `gh pr view 917 --json statusCheckRollup`，run `43913375220` | **6 项 failure**：`Python: lint, types and contract compatibility`、`Integration 3/12`、`Integration 6/12`、`Gate: Python tests and static checks`、`Gate: integration partitions and merged coverage`、`Gate: required checks for this change` |
| CI 失败本地复现 | `uv run mypy tests/integration/test_gateway_instance_reclaim_pg.py`；§7.2 的两个集成用例 | 1 mypy error；**2 failed**（`unknown=1`，日志含新增的 `execution failure without verified stop evidence`） |
| 反例重做（四组） | 见 §7.1 | 2 / 5 / 5（另 1 passed）/ 2 failed，全部还原 |
| 篡改还原 | `git status --porcelain` | 空（含临时探针删除后） |

被审查 head 的真实用例只覆盖"已释放全部租约 + 已验证释放"的组合（`test_gateway_instance_reclaim_pg.py:237-254`），
不覆盖"释放已验证 + 租约仍 `reserved`"（§3.1 的空缺）。我的网关侧集成跑（三部文件 71 passed）只覆盖
gateway 包，没有覆盖 backend 派发集成；CI 的两处分片失败与随后的一手复现（§7.2，在作者的
`/private/tmp/sn918` 工作树里只读运行）补上了这一层。

## 1. 作者自述逐条核对

| 自述 | 判定 | 依据 |
| --- | --- | --- |
| 判定表九行、只读、不发停止命令、不归还预算 | **一行不成立，其余成立** | `decide()` 的 `release` 分支排在 `holds_capacity` 之前（`domain/instance_reclaim.py:270-276`），"仍持 reserved 租约 → 不回收"（文档第 128 行）在同时有释放证据时为假；其余分支顺序与文档表逐行对应。集成用例断言三张表行数前后不变（`:288-302`）。 |
| 停止证据只走 node checkpoint → verified-stop → `whole_instance_stop_evidence` → `record_stop` → 唯一表 | **"唯一表"成立，"只有一条链/转换一次"不成立** | 表本身唯一，写入者有两个：网关 `record_stop`（`stop_sql.py:95`）与既有运维出口 `quarantine_release_sql._insert_stop_evidence`（`:167`，种类 `operator_verified_instance_terminated`）。回收答案的释放事实由 `NodeStopEvidenceSource.release_evidence` 直接构造（`node_stop_evidence.py:181-272`），不经过 `whole_instance_stop_evidence`；README `:32` 的 "converted once by `whole_instance_stop_evidence`" 对回收路径为假。 |
| `StopEvidenceKind` 无 sleep 成员，sleep 应答不算释放 | 成立 | 契约生成类型 6 成员无 sleep（`packages/contracts/.../execution_models.py`）；`ReleaseEvidenceKind` 的 `engine_sleep_ack` 只存在于 `voice_design_release.py:34-49`，从不入账本；反例见 §7 反例一。 |
| 观察时刻取数据库 `clock_timestamp()`，锚点取就绪与最近活动的较大值 | 决策成立，摘要混入第二个时钟 | `drain_sql.py:89-126` 的 `observed_at_us` 来自数据库；但 `ReclaimSnapshot.observed_at` 用进程时钟（`application/instance_reclaim.py:51,91`），且无任何消费者。 |
| 身份 / 世代 / 绑定 / fencing 核对 | 成立 | `_verify_authority`（`application/instance_reclaim.py:94-120`）先核对 host/gateway/记录存在/`max(last_fencing_token, enforced_minimum)`；节点端口再核对身份与 `fencing_token < 记录值` 拒绝（`node_stop_evidence.py:213-260`）。集成用例覆盖陈旧 token（`:272-287`）与伪代次。 |
| 无证据不得 `failed`：Job 保持 running、租约继续持有、终态交给协调路径 | 前两句成立，**第三句不成立** | 见 §4：协调与取消路径不接受终态 `failed`/`aborted` 的隔离，Job 既拿不到终态也不会进债务探针。 |
| 反例四组各红 | 成立（我的重做计数不同） | §7.1。 |
| （未自述）既有集成用例是否仍绿 | **不成立：两处回归** | 新门禁把 `test_voice_generation_dispatch_pg.py::test_engine_reported_timeout_is_projected_failed` 与 `test_job_dispatch_settlement_pg.py::test_tts_engine_failure_releases_capacity_through_the_stop_guard[postgresql]` 判成 `unknown`；作者只同步了单元 fake（§7.2）。 |
| 自报事故：反例验证时同名备份覆盖了 `domain/instance_reclaim.py`，之后按测试契约重写 | 未见重写残留，但留下了唯一的实质矛盾 | §6。 |

## 2. 真相源唯一性

1. **枚举没有第二份睡眠语义。** `StopEvidenceKind` 由契约生成，成员 `none` / `engine_abort_ack` /
   `engine_terminal_response` / `instance_terminated_by_controller` / `operator_verified_instance_terminated` /
   `unconfirmed_timeout`，没有 sleep 成员；睡眠应答只存在于 `ReleaseEvidenceKind`，而该枚举只在
   `VoiceDesignReleaseEvidence` 内部使用，从不写入 `execution_stop_evidence`。`is_verified` 恰好排除
   `none` 与 `unconfirmed_timeout`（`domain/submission.py:657-668`）。
2. **状态文档里的 `stop_evidence_kind` 只有一处派生。** `submit_sql._intent_from_row` 从
   `LEFT JOIN execution_stop_evidence` 取 `evidence_kind`，并在 `cleanup_state=complete` 而无持久化已验证
   停止时抛 `ValueError("a cleaned-up execution needs a durable verified stop")`（`submit_sql.py:681`）。
   backend 读到的种类因此不是第二份记忆，而是账本行的投影。
3. **表的写入者是两条既有车道，不是新造第三条。** 网关 `record_stop` 与 ops 出口
   `quarantine_release_sql` 都写同一张表、不同种类；本 PR 两者都没动。因此"唯一表"成立。
4. **回收路径的释放事实与账本不共享转换函数，但共享来源与校验。** `release_evidence` 与
   `termination_evidence` 调用同一个 `_fetch`（同一 HTTP 文档、同一 TLS/绑定/超时栈），再各自转换：
   前者只取 `process_or_container_exited` / `resources_released_confirmed` 与 `verified_at`，后者走
   `whole_instance_stop_evidence`。两者不会互相矛盾（同文档、同时间窗、同身份核对），但"转换一次"
   是设计意图而非现状。文档需改成"同一份 node checkpoint 文档的两个只读投影"。

结论：**没有第二套真相源；一处措辞夸大（§9 文档必修），没有代码级的分叉记忆。**

## 3. 回收证据攻击

逐条按任务给的攻击面走，结论先给：除 §3.1 外，其余攻击面都守住了。

### 3.1 已验证释放 + 仍持 `reserved` 租约 → `reclaimed`（F1，必修）

`decide()` 的顺序是 resident → quarantined → draining → **release** → `holds_capacity` → anchor → cooldown
（`domain/instance_reclaim.py:256-285`）：只要释放证据成立就返回 `RECLAIMED`，不再看 `holds_capacity`。
它的 docstring 却说"Release evidence is checked after the gateway's own unfinished work: a terminated
generation whose executions this gateway has not closed yet is not fully released here"（`:259-261`），
`reclaimed` 的 docstring 写"its capacity is free"（`:152-155`）。三处互斥。

真实 PostgreSQL 复现（临时集成用例，跑完已删除，`git status` 空）：

```
PROBE holds_capacity= True release_is_evidence= True state= reclaimed
      value= {'reclaim_state': 'reclaimed', 'refusal': None, 'idle_ms': None, ...}
```

即：同一个答案对象里，`holds_capacity=True` 与 `reclaimed` 同时为真，且 `refusal` 为空——调度器若按
`reclaimed` 理解为"容量已归还"就会误判。领域层一行即可复现：`decide(facts(release=stopped_evidence(),
holds_capacity=True))` 也返回 `reclaimed`。

成因不是设计取舍：`holds_capacity` 就是"本网关自己的未完成工作"（`ReclaimFacts` 把它与 drain 三桶并列，
文档表也把它列在"不回收"）。测试里唯一同时出现"释放证据 + 网关工作"的用例是 quarantine 版
（`test_a_terminated_generation_still_holding_gateway_work_is_not_reported_reclaimed`，`:195`）；
`holds_capacity` 的两处覆盖（参数项 `:114`、集成 `test_a_still_reserved_lease_...`，`:203-219`）都只给
`release=None`，组合从未被断言。
这不是理论形状：#906 的 `completed-without-release` 正是"执行结束、租约仍 `reserved`"，而实例终止后
`record_stop` 与 backend 的租约释放是两个事务——窗口内就是"释放已验证 + 租约仍 reserved"。

修法：把 `if facts.holds_capacity:` 提到 `release` 之前（或 `RECLAIMED` 追加 `not facts.holds_capacity`），
并补一条"释放证据 + reserved 租约 → `HELD/CAPACITY_HELD`"用例。不会死锁：租约释放的前置是已持久化的
停止证据，释放证据存在时 backend 的 `lease_release` 可以推进；窗口结束后答案转 `reclaimed`，语义才与
`reclaimed` 的 docstring 一致。

### 3.2 时钟回拨 / 锚点在观察时刻之后

`ReclaimRecord.__post_init__` 与 `ReclaimFacts.__post_init__` 都把 `idle_anchor > observed_at`、`release.observed_at >
observed_at` 当拒绝（`domain/instance_reclaim.py:108-118,200-205`），不折成"不空闲"，符合
"读失败不能读成不空闲"的自述。唯一瑕疵：抛的是 `InvalidExecutionRequestError`（业务 4xx 类，`submission.py:261`），数据/时钟异常会被未来路由映射成 422，见 §8。

### 3.3 `ready_at_us` 缺失 / 无活动

两者皆无 → `ACTIVITY_UNKNOWN` 且永不因空闲回收，与文档一致。副作用是"冷加载失败、从未就绪"的代次
在这张表里没有出口，需要加载失败路径自己处理——文档没点名责任方，见 §8。

### 3.4 `activity_unknown` 是否可被跳过

不能。锚点为 `None` 时：有合法释放 → `RECLAIMED`（此时不需要锚点，正确）；有未确认释放 →
`RELEASE_UNCONFIRMED`；无释放 → `ACTIVITY_UNKNOWN`。除 §3.1 外没有把 `None` 锚点读成 `reclaim_due` 的路径。

### 3.5 `cooldown_active` 边界

`idle_ms < cooldown` 才 held，恰好相等返回 `reclaim_due`（`:280-285`），与文档"空闲时长小于
`idle_reclaim_cooldown_ms` → 冷却中"一致；单测 `test_the_cooldown_boundary_is_inclusive_at_the_manifest_value`
（`:136`）覆盖。`idle_ms` 用 `(observed - anchor) // _MS` 截断，不引入负值。

### 3.6 其余攻击面

- drain 三桶 → `DRAINING`/`QUARANTINED` 优先于一切，成立。
- sleep 应答、部分终止、非 200、404/401/500、未配置绑定 → `None` 或 `RELEASE_UNCONFIRMED`，成立
  （`tests/unit/gateway/test_node_stop_evidence.py:393-479`、`test_voice_design_release.py:123-233`）。
- 另一代次/另一池/另一绑定/更低 fencing → 端口层拒绝，成立。
- 只读：`read_reclaim_record` 走 `_run(read_only=True)`（`submit_postgres.py:372-376`）；集成用例统计三表行数
  前后不变；对照 `_SELECT_RECLAIM`（`drain_sql.py:89-126`）无 UPDATE/INSERT/DELETE。
- 授权面：新查询只读 `engine_instances` / `admission_leases` / `submit_intents`，集成用例以受限网关角色连接通过，
  在既有 grant 内。

## 4. 门禁有界性（"无停止证据不得 failed"）

先说清它何时可达，这决定这条算不算阻塞。**同一份代码内不可达**：

1. 契约矩阵：`failed ⇒ cleanup ∈ {not_required, complete}`；`aborted ⇒ complete`；`failed+complete` 的
   `stop_evidence_kind` 只允许 3 种（`contracts/internal/execution-control.openapi.yaml:892-940`）。
2. 网关域：`RecordStop` 要求 `is_verified`（两 flag 且种类非 `none`/`unconfirmed_timeout`，`submission.py:983-994`）；
   `SubmissionIntent.validate()` 要求 `not_required` 无 `dispatched_at`/`engine_request_id`、`complete` 必须有
   `dispatched_at`（`:810-835`）。网关自身转移因此产不出被门禁拒绝的形状。
3. 读路径：`cleanup_state=complete` 而无持久化已验证停止时 `_intent_from_row` 直接抛错（`submit_sql.py:681`），
   不会变成状态文档，而是 500。

但这只证明**同代码版本的网关转移产不出它**。跨版本/非契约状态文档仍然可达，而且比看上去近：
`ExecutionStatusResponse` 要求 `cleanup_state` 与 `stop_evidence_kind`（`contracts/internal/execution-control.openapi.yaml:1225-1250`），
backend 的 `query_document` 却明确不做 schema 投影（`backend/.../infrastructure/gateway_execution_client.py:433-436`），
状态文档是否合规完全靠网关自觉。head 自己的 CI 就踩到了：两个既有集成用例的 fake 只回
`{submit_intent_id, execution_state, error_code}`（`test_voice_generation_dispatch_pg.py:217-224`、
`test_job_dispatch_settlement_pg.py:352-361`），门禁立刻把 `failed` 判成 `unknown`（§7.2）。所以这条门禁是
"非契约文档的唯一防线"，方向正确：宁可挂住，也不假失败归还容量。

**但一旦触发，没有任何有界出口。** 逐条核对（`job_dispatcher.py:385-402`）：

| 候选出口 | 实测结论 |
| --- | --- |
| 派发重试 | 每次仍返回 `unknown`；`claim_version` 超过上限后落 `dispatch_attempts_exhausted`，仍是 `unknown`，不终态。 |
| 派发截止 `job_timeout` | `_follow` 在状态判定后进入，已被本分支提前 return，走不到。 |
| Job `expired` 写者 | `expired` 在 Job 状态词汇里（`domain/public_jobs.py:21`），但没有写者把它写到 Job 上（`git grep` 命中的都是产物/资产过期）。 |
| 取消协调 worker | `stop_unconfirmed` → `UnconfirmedCancellationSettlement.settle` → `isolate_sync`；`quarantine()` 只接受 `execution == ("unknown_quarantined","quarantined")`（`domain/leases.py:150-167`），终态 `failed` 恒被 `execution_cleanup_pending` 拒绝，Job 停在 `cancelling`。该结算类自己的 docstring 也只列了"仍在执行/仍在停止/已成功"三种被拒形状（`job_cancellation_runtime.py:258-264`），没有这一类。 |
| `cancellation_debt` 探针 | 只查 `speech_jobs.state='cancelling'` 且有 fence 的 Job（`readiness.py:335-385`）；`running` 的看不见。 |

也就是说：新增的是"契约违例时的永久挂住 + 零可见"，换掉了"假失败 + 提前归还容量"。前者安全，但没有满足
#906 建立的有界出口标准，而且 `job_dispatcher.py:391-392` 的注释与文档第 138 行"终态交给协调路径"
是不成立的陈述。判定：**门禁保留，必须补可见性与处置说明（§9 M4），不要求本 PR 新增操作员命令。**

## 5. 未接线机制判定

- 生产调用者：零。`InstanceReclaimService`、`NodeStopEvidenceSource.release_evidence`、
  `VoiceDesignStopReceipt.from_instance_termination` / `verified_stop` 只在测试里被引用；`interfaces/` 无引用、
  `app.py` 无装配、无 settings、无路由（`rg "reclaim|release_evidence|from_instance_termination"` 的结果全部落在
  本 PR 新增文件与测试）。
- 配置前置：唯一的释放端口实现 `NodeStopEvidenceSource` 只在 `recovery_config_file` 配置下构造
  （`infrastructure/recovery_configuration.py:86-110`），且 `max_in_flight=1`——接线后回收与恢复会同抢一个槽。
  `idle_reclaim_cooldown_ms` 也没有任何生产读取者：服务只校验调用方传入值在契约边界内
  （`domain/instance_reclaim.py:99-105`），仓库里除契约、示例清单与本 PR 测试外无人读该键。
- 因此 README `:25-33` 的"the instance-reclaim service reads … the manifest `idle_reclaim_cooldown_ms`"
  与治理文档第 134 行"该答案当前只在 gateway 进程内产生"都把**未接线**写成了**已生效**；同一个 diff 还删掉了
  原句"这些结构约束不证明调度或回收已接通"（`runtime-governance.md` 第 140 行旧文）。

判定：**以中间切片合并可接受**（与 drain 的"先服务、后接线"同型；不写行、不发停止命令、不归还预算，
死代码不会误伤），但必须如实写成"未接线 + 接线前置"，不能反过来。否则读者会把 §3 的判定表当成线上行为。

## 6. 被覆盖文件的自洽性

自报事故（反例验证时同名备份覆盖 `domain/instance_reclaim.py`，之后按测试契约重写）没有留下半旧副本：

- 无重复定义、无未用导入/常量，`__all__`（`:296-305`）与定义一致；`ReclaimRequest`/`ReclaimRecord`/
  `ReclaimFacts`/`ReclaimDecision` 的校验互相一致（同一 cooldown 边界、同一 `Identifier` 正则、同一时区要求）。
- 分支顺序与文档表逐行对应，只有 `release`/`holds_capacity` 一对被对调——正是 §3.1。
- 重写痕迹集中在两处"值对象多带了一个字段"：`ReclaimSnapshot.observed_at`（进程时钟，无消费者）、
  `verified_failure_evidence` 返回值只被当布尔用（`job_dispatcher.py:386-387`）。都不影响判定，属 §8。
- `decide()` 的 docstring 与实现相反这一点本身是"重写后按测试契约对齐、但没回头读 docstring"的典型指纹；
  修 §3.1 时把 docstring 一起校正即可，别的无需重写。

## 7. 反例重做与既有用例回归

### 7.1 判据反例（四组，全部红）

每组都是"改坏 → 跑定向用例 → 从 `/tmp` 备份还原 → `git status --porcelain` 空"：

| 反例 | 变异 | 定向命令 | 结果 |
| --- | --- | --- | --- |
| 一：sleep 应答当释放 | `engine_sleep_ack` 置 `exited/release_confirmed=True`，并把 `ENGINE_SLEEP_ACK` 加进 `is_release_evidence` 的种类集 | `test_an_engine_sleep_acknowledgment_is_not_release_evidence`、`test_an_engine_sleep_acknowledgment_never_reclaims_the_generation` | **2 failed**（`assert True is False`） |
| 二：去掉失败证据门禁 | `verified_failure_evidence` 无条件返回 `stop_evidence_kind` | `test_dispatch_failure_evidence.py`（4 条）+ `test_job_dispatch.py` 两条门禁用例 | **5 failed**（`failed=1`） |
| 三：回执接受部分终止 | 同时去掉 `from_instance_termination` 的 `is_complete`/种类校验与 `__post_init__` 的终止完整性校验 | `test_a_partial_or_sleep_shaped_termination_cannot_build_a_receipt`、`test_a_missing_termination_cannot_build_a_receipt` | **5 failed, 1 passed**（只去工厂校验时仍被 `__post_init__` 挡住，说明是双层防护，红的是两层都去掉） |
| 四：隔离优先级 | 删掉 `decide()` 的 `QUARANTINED` 分支 | `test_a_quarantined_generation_outranks_unfinished_work`、`test_a_terminated_generation_still_holding_gateway_work_is_not_reported_reclaimed` | **2 failed**（`HELD` 变 `RECLAIMED`） |

另外逐项核对了"既有守护没有被放宽"：`stop_sql.py`、`quarantine_release*`、`readiness.py`、
`whole_instance_stop_evidence` 均无 diff；`node_stop_evidence._query` → `_fetch` 是抽取等价重构（请求构造、
`validate_request`、超时、状态码、`_evidence` 转换逐行相同），恢复扫描的行为未变；backend 侧只新增常量与前置
判断，`gateway_failure_code` 与派发结果分类未改。改动测试 fixture 补的 `stop_evidence_kind`/`cleanup_state`
默认值是契约合法形状，不是放宽断言。

### 7.2 既有集成用例被新门禁打红（CI 红，单独阻塞）

head 的 CI 不是绿的，原因分两类，都在本地一手复现：

1. **mypy 一处**：`tests/integration/test_gateway_instance_reclaim_pg.py:95` 的 `live` fixture 注解为
   `-> Harness` 却 `yield`，严格模式判 "The return type of a generator function should be
   Generator or one of its supertypes"。CI 的 `Python: lint, types and contract compatibility` 因此失败。
2. **两个既有 PG 集成用例被新门禁判成 `unknown`**：
   - `test_voice_generation_dispatch_pg.py::test_engine_reported_timeout_is_projected_failed`
     （`:495`，`assert batch.failed == 1`，实际 `unknown=1`）；
   - `test_job_dispatch_settlement_pg.py::test_tts_engine_failure_releases_capacity_through_the_stop_guard[postgresql]`
     （`:439`，同样 `failed=0, unknown=1`）。
   根因是两处 fake 的**状态文档**不完整：`_status()` 只返回 `submit_intent_id`/`execution_state`/`error_code`
   （`:217-224`），settlement 的 fake 虽把 `failed/not_required` 写进库，却只投影出
   `{submit_intent_id, execution_state, error_code}`（`:352-361`），都没有 `cleanup_state` 与
   `stop_evidence_kind`。门禁按契约要求 `kind == "none"` + `not_required` 才放行，于是拒绝——日志里就是新增的
   `execution failure without verified stop evidence`（`job_dispatcher.py:393`）。
   修法是把 fake 的状态文档补成契约形状（`cleanup_state`、`stop_evidence_kind`，以及 `not_required` 分支的
   `dispatched_at`/`engine_request_id` 为 null），**不是放宽断言**。

我的网关侧集成跑（71 passed）与作者的单元套件都覆盖不到这两处，所以这条只能靠 CI 或跑全量集成发现；
CI 在 head `284652ff` 上是红的，合并前必须绿。

## 8. 可接受但需记录

1. **证据种类词汇第三份副本（F6）。** 新增 `job_dispatch.VERIFIED_STOP_EVIDENCE_KINDS`（`:507-515`）与
   `gateway_job_cancellation._STOP_EVIDENCE`（`:30-37`）、`gateway_cancellation_confirmation._STOP_EVIDENCE`
   （`:37-44`）逐字相同，违反"不维护独立语义副本"与自述的"复用而非新造"。建议 infra 两处 import 领域常量
   （依赖方向允许），本 PR 可留待返修。
2. **`holds_capacity` 只认 `reserved`。** 租约状态词汇是 5 个（`backend/.../domain/leases.py:49`），
   `executing`/`stopping` 也占容量；今天没有写者写这两个状态（`rg "SET lease_state"` 只看到 `released`
   与 quarantine/release 的状态），属潜在。若词汇表保持，建议改 `lease_state != "released"`。
3. **第二个时钟。** `ReclaimSnapshot.observed_at` 用进程时钟，决策用数据库时钟，字段无消费者；
   留着会让未来的路由把两个时刻混用，建议删除或改名注明用途。
4. **异常类别。** `ReclaimRecord` 的时钟/数据异常抛业务 4xx 类 `InvalidExecutionRequestError`；
   接线成 HTTP 路由后会变成 422（客户端错误）而不是存储/数据错误，建议换成 store 类错误。
5. **resident / quarantined 也会打节点。** `evaluate` 先取释放事实再判定（`application/instance_reclaim.py:84-85`），
   前两行本来就能直接 held；接线后每个 resident 代次都会产生一次无用 HTTP（并与恢复扫描共享
   `max_in_flight=1`）。建议按 `loading_policy`/drain 结果短路。
6. **门禁比契约略宽。** `failed+complete` 的契约允许 3 种证据，门禁接受了 4 种（含 `engine_abort_ack`）；
   方向安全，但"只接受两类形状"的表述与契约集合不完全一致。
7. **`verified_failure_evidence` 的返回值只当布尔用。** 调用方只判 `is None`，返回种类无消费者；
   要么用上（进 warning 的结构化字段），要么返回 `bool`。
8. **锚点包含 `updated_at_us`，而网关自己会推进它。** `record_stop` 写 `updated_at_us`（`stop_sql.py:95-106`），
   所以"已验证停止"之后 `reclaim_due` 的冷却会重新起算；`release` 排在锚点之前正是为此，属保守设计。
   但文档"最近一次执行活动时刻（三列最大）"应改成"最近一次记录写入时刻"，并注明包含网关记账。
9. **集成覆盖的空缺。** reclaimed 用例先把所有租约释放（`:237-254`），reserved 用例没有释放证据；
   也没有用例覆盖 `record_stop` 推进 `updated_at_us` 后"释放证据先于锚点"的行为——这两处取舍都只写在注释里。
10. **冷加载失败代次没有出口。** 无就绪时刻、无活动的代次永远 `held`；文档没写这类由加载失败路径处置，
    接线时需要点名责任方。

## 9. 必须修清单

- **M1（代码，阻塞）** `decide()` 的 `holds_capacity` 必须先生效：把 `if facts.holds_capacity:` 移到 `release`
  之前，或让 `RECLAIMED` 要求 `not facts.holds_capacity`；同时补
  `decide(facts(release=stopped_evidence(), holds_capacity=True)) is HELD/CAPACITY_HELD` 的领域用例
  （集成层可选补一条真实 PG 的 reserved+release）。修完 `reclaimed` 的 docstring、模块 docstring 与
  `runtime-governance.md:128` 才三处一致。
- **M2（文档事实，阻塞）** 四处不实陈述必须改：
  1. `docs/architecture/runtime-governance.md:134` "该答案当前只在 gateway 进程内产生" → 写明服务已实现但
     尚无生产调用者/路由，接线前置是什么（释放端口需 recovery 配置、cooldown 由调用方给、与恢复扫描共享槽）。
  2. 同文件 `:136` "释放证据只有一条链 … `whole_instance_stop_evidence` 转成实例终止证据" → 保留"唯一
     `execution_stop_evidence` 表"的表述，删掉/改写"只有一条链"与"转换一次"：既有运维出口也写同表；回收
     答案走同一份 checkpoint 文档的另一个只读投影。
  3. 同文件 `:136` "有界重试沿用 … backend 的取消协调 worker" 与 `:138` "终态交给协调路径" → 对
     `stop_evidence_unverified` 形态不成立（§4），必须删掉或改成"无有界出口，按生产端契约违例处置"。
  4. 同文件 `:140` 被删掉的"这些结构约束不证明调度或回收已接通"应在未接线期间恢复；README `:25-33` 的
     "reads … the manifest `idle_reclaim_cooldown_ms`" 改成"校验调用方传入的清单值"。
- **M3（CI，阻塞）** 修绿 head 的 CI 再合：`test_gateway_instance_reclaim_pg.py:95` 的 fixture 注解改为
  `Iterator[Harness]`；两个集成 fake 的状态文档补上契约要求的 `cleanup_state`/`stop_evidence_kind`
  （以及 `not_required` 分支的 `dispatched_at`/`engine_request_id` 为 null），断言不动。
- **M4（可观测/处置，阻塞）** `stop_evidence_unverified` 形态必须有可见性和处置说明：给它一个稳定的事件名
  （现在只有自由文本 `warning` + `extra`），并在运行治理文档里写明"出现在 `running` 的 Job 不会进
  `cancellation_debt`，处置是修生产端并使该状态合法；本版本不提供操作员命令"，或把它纳入既有债务探针
  （后者是设计决定，可与作者商定）。`job_dispatcher.py:391-392` 的注释同批修改。

**不阻塞但建议同轮**：§8 第 1 条（集中 4 种证据词汇）、第 5 条（resident 短路）、第 3 条（删第二个时钟）。

---

## 10. 返修复核（`5032b15b`）

**结论：现在仍不能合。** M1 已关闭、head 的 CI 34 项检查全 SUCCESS，但 M4 新增的 `stop_evidence_debt`
探针在超时路径上会把 `/readyz` 变成 503（§10.2 一手实测），与它自己写的"永不因此让 /readyz 失败"和
`docs/components/production-jobs.md:413` 列出的 `unavailable (timeout)` 形态相反——这正是 #888 的形态；
M2 也还有两处"回收路径经同一 `whole_instance_stop_evidence` 转换 / 读这张表"的相反表述（§10.4）。
三处都是几行改动，修完可以合；其余为可接受但需记录（§10.5）。

复核范围：只审 M4 探针、M3 两处 fake、M2 四处文档的返修 delta，外带对 M1 的抽查；其余不重审。
复核基线 `5032b15b`（`284652ff` → `5032b15b` 为 +864/−40、16 文件），PR #917 head，
`gh pr view 917` 的 34 项非跳过检查全 `SUCCESS`。本地：`tests/unit/backend/test_stop_evidence_debt.py`
等 5 个文件 **113 passed**；`test_stop_evidence_debt_pg.py` 与两处修复过的集成用例 **8 passed**；
`test_gateway_instance_reclaim_pg.py` **12 passed**；`mypy` 对 4 个改动文件 clean。delta 代码在
`/private/tmp/sn919` 以 detached head 检出运行，跑完已切回审查分支，工作树干净。

### 10.1 M1 抽查（已关闭）

`decide()` 现在 `holds_capacity` 在 `release` 之前（`domain/instance_reclaim.py:286`），docstring 把
顺序、以及"停止生产者的事实与准入租约是两份不同记录、取弱者定答案"写成六条判定说明（`:259-279`）；
`reclaimed`/`CAPACITY_HELD` 的 docstring 同步改正。新增用例覆盖：领域层 reserved+release、
"表序高于释放报告的每一行"参数化、sleep 应答+reserved，真实 PG 一条
（`test_gateway_instance_reclaim_pg.py:257-284`）。我上一轮的攻击形态
（`decide(facts(release=stopped_evidence(), holds_capacity=True))`）现在返回 `HELD/CAPACITY_HELD`。
M1 关闭，无残留。

### 10.2 M4 探针：准入门 / 上界 / 代价 / 授权 / 判据 / 宽限

**准入门（关键）：DB 异常与权限缺失只影响 detail，超时会改聚合状态。** 实测（`ReadinessService`
包住探针，与 `app.py:283` 同构）：

| 失败模式 | 实测 `ready` / `status` / `detail` | 判定 |
| --- | --- | --- |
| 连接被拒（端口关闭） | `True` / `ok` / `stop_evidence_debt: unavailable (connection)` | 只影响 detail ✓ |
| 权限缺失（REVOKE submit_intents） | `True` / `ok` / `stop_evidence_debt: unavailable (permission)` | 只影响 detail ✓（新集成用例已钉） |
| 只读 DSN 卡住（黑洞 TCP） | **`False` / `failed` / `timed out`** | **改聚合状态 ✗** |

根因是两层超时用同一个预算：探针自己做 `async with asyncio.timeout(self._timeout_seconds), self._lock:`
（`readiness.py:514`），服务层 `_inspect_probe` 又包了
`async with asyncio.timeout(self._timeout_seconds)`（`speech/application/readiness.py:69`），而
`app.py:283` 给两者都传 `settings.readiness_timeout_seconds`（默认 3.0 s，`settings.py:52`）。外层先入、
先到期，内层的 `TimeoutError` 永远没机会被 `check()` 的 `except Exception` 转成 detail；外层把
`CancelledError` 转成 `TimeoutError` 后记 `FAILED`。后果是只读 DSN 卡住超过一个就绪周期就 `/readyz`
503、`diagnostics` 报 `unready`——正是 #888 那个坑。既有 `CancellationDebtProbe` 同构、同样被实测为
`failed`，属既存缺陷；但本 PR 新增了第二个实例，并在探针 docstring、`runtime-governance.md:140` 与
`production-jobs.md:413` 三处明确声明不会发生，所以要么一起修，要么删这三处声明。修法很小：两个探针的内层预算取服务的 0.8×（或服务层放大 1.25×），内层 `TimeoutError`
就会变成 `unavailable (timeout)`；再补一条"慢探针不得改聚合"的用例。

**detail 上界：有行数上界，没有字节上界。** `limit=96`（`readiness.py:489`），代码只显示前 3 个 Job，
其余 `(+N more)`（`:448`）；单行是 intent 标识符（契约上限 128 字符）+ 三四个短枚举。实测最坏
（单 Job 96 个 128 字符 intent）**17 704 字符**，3 Job×96 为 17 963；直接传 5 000 行时 1 062 字符
（3-Job 截断生效）。没有字节棘轮，单测只钉了"9 个单 intent Job < 500"（`test_stop_evidence_debt.py:82`）。
同族 `cancellation_debt` 同形（96 行×约 120 字符）。建议给 detail 一个硬截断（如 2 000 字符，保留
前缀与每个 Job 的前几个 intent）并加一条最长标识符用例。

**查询代价：每个就绪周期全表扫一次 `speech_jobs`，O(全部 Job) 而非 O(债务)。** 实测计划：
20 000 个终态 Job + 1 个债务 Job 时 `Seq Scan on speech_jobs`、`Rows Removed by Filter: 20000`、
1.1 ms；100 000 个时 `Parallel Seq Scan`、执行 8.8 ms、探针端到端 25 ms（含连接）。索引面：
`speech_jobs(updated_at_us, job_id)`（migration 0048）存在，但 `state='running'` 无法走索引，
规划器直接顺扫；三个连接键都走唯一索引（leases `(operation_id, attempt_id, stage_id)`、intents
`lease_id`、evidence 主键），连接本身很便宜。`/readyz` 每几秒一次时会按表大小线性放大
（100 万 Job 约 90 ms/次，且每次都扫过整表缓存）；同族 `cancellation_debt` 的过滤起点是 fence 表
（`FROM fences … JOIN speech_jobs`），本探针的过滤起点是 `speech_jobs` 全表；就绪周期里这一次全表扫描
是本 PR 新增的开销。建议 `(state, updated_at_us)` 部分索引，或把两个债务查询合并成
一次。

**列级授权：四列都是最小用途，但没有逐列等值校验。** `speech_jobs.updated_at_us` 只用于宽限过滤
（`:414-415`）；`submit_intents.cleanup_state` 用于非派发判定（`:399`）并进入 detail；
`submit_intents.dispatched_at_us`、`submit_intents.engine_request_id` 只用于非派发判定（`:400-401`）。
授权元组（`job_cancellation_fence_schema.py:130-145`）没有把 `request_document`、`command_digest`
带进来，证据表沿用既有的 `execution_stopped/resources_released`，最小面成立。`refuse_column_privileges`
一类的"逐列等价比对"只用于运行时身份（artifact/audit/binding），这个只读诊断身份没有对应校验：保护是
显式列清单 + 集成用例按同一清单跑通 + 一条 REVOKE 降级用例。可接受，但升级时旧授权不会自动带新列，
`stop_evidence_debt` 会整体降级成 `unavailable (permission)`（`cancellation_debt` 不受影响），
deploy 文档的"升级必做"只写了 `cancellation_debt` 的部分降级，没写这一种（§10.5 的 M4-c）。

**判据等价性：域能产生的形状上等价；四处差异都落在不可达形状。** 逐条对照
`verified_failure_evidence`（`domain/job_dispatch.py:537-548`）与探针 SQL（`readiness.py:395-417`）：

| 差异 | gate | 探针 | 后果 | 可达 |
| --- | --- | --- | --- | --- |
| 已验证臂是否要求 `cleanup=complete` | 要求 | 不要求 | 已验证种类+非 complete：gate 挂住、探针静默 | 不可达（矩阵 + `not_required` 无证据行 + 读路径 500） |
| 已验证臂是否校验两 flag | 只看种类 | 要求 `stopped=1`/`released=1` | 种类已验证但 flag 为 0：gate 放行、探针报债 | 不可达（两个写者都写 1/1） |
| 非派发臂是否要求 `kind=none` | 要求 | 只比三列 | `not_required` 却有证据行：gate 拒绝、探针静默 | 不可达（同上） |
| 租约状态 | 挂住即租约仍持有 | 排除 `released`/`quarantined` | 租约已归还但 gate 仍挂住 | 不可达（无证据无法归还租约） |

结论：门禁的拒绝集 = 探针的指出集（加上不可达形状），"门禁挂了探针看不见"在可产出的状态上不成立。
但这是**第四份**四种已验证种类的副本（`readiness.py:378-383`），与 `job_dispatch.py:507-515` 及两处
infra 副本逐字相同；探针的正确性依赖这份集合与门禁一致，建议直接 import 领域常量
（infrastructure → domain 允许），否则未来漂移会立刻变成两边不一致。

**宽限：锚点是 claim 时刻，滞后上界 300 s，不会长期看不见。** 探针用 `j.updated_at_us` 计时
（`:414-415`，默认 300 s）。`running` 期间只有 `_mark_running` 写这一列（`job_dispatch_postgres.py:268`，
claim 时）；`_renew`（`:294-302`）与 `_record_progress`（`:319-380`）都不碰 job 行；取消与终态写
`updated_at_us` 后状态已不满足 `state='running'`。所以滞后 =
`max(0, 300 s − (失败时刻 − claim 时刻))`，上界 300 s；运行超过 5 分钟的 Job 立即出现，不存在"刚挂住
长期看不见"。副作用是语义与 `cancellation_debt` 的"确认窗口 + 退避"不同：这里被等的是永久形状
（failed/aborted 无证据不会被 sweep 收掉，sweep 只处理 `unknown_quarantined`），宽限只是延迟可见；
对运行很久的 Job 等于没有宽限，但也不会误报。实现无害，记录。

### 10.3 M3 两处 fake 的字段对照

按 `ExecutionStatusResponse` 的 23 个必填项逐项对照（`contracts/internal/execution-control.openapi.yaml`
的 `required`，`:1225-1250`），两处 fake 的状态文档提供 5 个必填 + 2 个可选：

| 类别 | 字段 |
| --- | --- |
| 已提供（必填 5） | `submit_intent_id`、`execution_state`、`cleanup_state`、`stop_evidence_kind`、`dispatched_at` |
| 已提供（可选 2） | `engine_request_id`、`error_code` |
| 仍缺（必填 18） | `lease_id`、`operation_id`、`attempt_id`、`stage_id`、`workspace_id`、`pool_id`、`host_node_id`、`gateway_id`、`instance_id`、`instance_generation`、`binding_revision_id`、`policy_revision_id`、`voice_revision_id`、`fencing_token`、`command_digest`、`deadline_at`、`sample_rate_hz`、`updated_at` |

实测两份文档都过不了 `parse("ExecutionStatusResponse", …)`。也就是说补的正是门禁读的四列
（`cleanup_state`/`stop_evidence_kind`/`dispatched_at`/`engine_request_id`），不是契约必填面；
`_status` docstring 里"One contract-valid execution document"的自我描述不成立。另有一处内部矛盾：
voice fake 的 `failed` 给 `cleanup=complete` + `engine_terminal_response`，却把 `dispatched_at` 置 null——
契约的 `complete ⇒ dispatched_at` 形状产不出这种文档，门禁也不校验这组交叉字段（上一轮记录的
"门禁比契约宽"）。正面是两臂现在真的被端到端执行：settlement fake 的 `failed` 走
`not_required`+`none`、voice fake 的 `failed` 走 complete+已验证种类，两条都断言 `failed=1`。
判定：**"补了但不完整，恰好够过门禁"成立**；修法是把 fake 文档过一遍 `parse`（或复用生成模型的
构造器），或删掉"contract-valid"的说法、只声称"门禁读取的字段符合契约"。不阻塞。

### 10.4 M2 四处文档原文

| 位置 | 改后原文 | 与实现对照 |
| --- | --- | --- |
| `runtime-governance.md:138` | "`execution_stop_evidence` 由两条既有车道写入：gateway 的 `record_stop` … 与 backend 的隔离释放车道 … 回收路径只读这张表与 verified-stop 投影，不新增写者、不新增表。" | 车道数 ✓（全仓只有 `stop_sql.py:35`、`quarantine_release_sql.py:167` 两个 INSERT 者）；**"回收路径只读这张表" ✗**——回收路径（`application/instance_reclaim.py` + `drain_sql.py`）不读 `execution_stop_evidence`，该表在 gateway 只被 submit/stop/reader_authority 引用 |
| `runtime-governance.md:136` | "没有公开路由，也没有生产调用方 … 释放报告取自既有 verified-stop 投影（恢复扫描已在生产使用的同一接口与同一 `whole_instance_stop_evidence` 转换）。" | "无生产调用方" ✓（`InstanceReclaimService` 仍零生产构造）；"同一接口" ✓（同一 node verified-stop 路由，`node_stop_evidence.py:318-338`）；**"同一 `whole_instance_stop_evidence` 转换" ✗**——`release_evidence` 直接构造 `VoiceDesignReleaseEvidence`（`:266-271`），该转换器只在 `recover_executions.py:119` 与测试/探针里 |
| `runtime-governance.md:140` | "既有取消协调与 `cancellation_debt` 只覆盖 `cancelling` 且有 fence 的 Job，**不会**替 `running` 的这类 Job 收尾。" | ✓，与 `_DEBT` 的 `FROM fences … WHERE j.state='cancelling'`（`readiness.py:346-369`）一致 |
| `runtime-governance.md:142` | "这些结构约束不证明调度或回收已接通" | ✓ 已恢复 |

同根的两处仍在：`execution-gateway/README.md:37` "Release evidence is … converted by
`whole_instance_stop_evidence`" ✗（同上）；`domain/voice_design_release.py:9-14` 说收据委托转换器
"which is what appends the row"（转换器是纯函数，不写行）以及"回收判定与恢复扫描读同一 receipt" ✗
（回收读 `release_evidence` 的释放事实，不经收据/转换器）。三处应统一改成"回收答案读同一份 node
verified-stop 文档的独立投影，不经 `whole_instance_stop_evidence`，也不读 `execution_stop_evidence`"。
判定：M2 只改了一半——四条里两条的"回收路径"从句仍与实现相反。

### 10.5 剩余必须修与记录

**必须修（阻塞）：**

- **M4-a（代码）：** 超时路径必须只影响 detail。两个探针的内层预算严格小于服务预算（如 0.8×），
  让内层 `TimeoutError` 走 `unavailable (timeout)`；补一条"卡住的只读 DSN 不得改聚合状态"的实测用例。
  两处同修，否则删掉 docstring 与 `production-jobs.md:413` 里"永不因此失败/`unavailable (timeout)`"的声明。
- **M4-b（文档事实）：** `runtime-governance.md:136`、`:138`、`README.md:37`、
  `domain/voice_design_release.py` 模块 docstring 的"同一转换 / 只读这张表 / 同一 receipt"三处表述。

**建议同轮（不阻塞）：**

- **M4-c（部署说明）：** `deploy/README.md` 与 `docs/operations/compose-minimal.md` 的"升级必做"补一句：
  重跑 `cancellation_debt_reader_grants` 之后 `stop_evidence_debt` 才有四列授权，否则它报
  `unavailable (permission)`（`cancellation_debt` 不受影响）。
- **detail 硬截断** 与最长标识符用例（实测 17 704 字符，无字节上界）。
- **探针全扫 `speech_jobs`**：`(state, updated_at_us)` 部分索引或合并两个债务查询（实测 100k Job
  ≈ 8.8 ms/次，1M ≈ 90 ms/次）。
- **四种已验证种类的第四份副本**：`readiness.py:378-383` 改为 import 领域常量。
- **fake 字段**：过 `parse("ExecutionStatusResponse", …)`，并修 voice fake 的
  `complete`+`dispatched_at=null` 交叉矛盾。
- **宽限语义**：锚点是 claim 时刻，对运行超过 5 分钟的 Job 等于无宽限；与 `cancellation_debt` 的
  "确认窗口"不同，如要一致需换锚点（例如取"最近一次失败观察到"的持久时刻）。
- **`_describe_stop_evidence_debt(..., missing=...)`** 无生产调用（探针没有 job-level 回退），死参数。
