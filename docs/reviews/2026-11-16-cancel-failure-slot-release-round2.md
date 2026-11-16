# 取消失败终态判定第二轮的非作者审查（PR #888，head `e4f4e7a3`）

审查对象：PR #888 `fix/cancel-releases-slots`，第二轮 head `e4f4e7a3`（上一轮已审 `abefdf55`）。
审查人不是作者。范围仍是租约、预算与终态语义，重点是作者本轮返修后**新引入**的攻击面：
「`failed/cancellation_unconfirmed` ⇒ 每条租约已落地」不变量、`cancellation_debt` 探针、
响应头移除审批类、`LEAST(attempts+1, 64)`。

结论：**不能合**。上一轮两个 P1 里，「部分隔离仍写终态」与「谎报 `quarantined`」确实修好了，
逐条隔离、门前闸、读面去谎都经得起实测；但本轮引入了一个**部署级阻断**（新探针在参考部署的
API 身份上权限不足，`/readyz` 永返 503），并且「执行已终态但无停止证据」这一类受理现在**永久
停在 `cancelling`**，与本文件自己写的「必须在有界时间内到达明确的算力结论，不能停在
`cancelling`」直接冲突。两条都需要返修。

## 0. 复核记录

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

## 1. 不变量攻击：`failed/cancellation_unconfirmed` ⇒ 每条租约已落地

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

## 2. `cancellation_debt` 探针：判据正确，但它自己会让 `/readyz` 永久 503

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

## 3. 响应头移除审批类：形态正确，留了一个不会过期的口子

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

## 4. 反例重做（3 条，逐项 `git checkout --` 还原，收尾 `git status --porcelain` 为空）

| 反例 | 我的改动 | 实测红 | 作者自述 |
| --- | --- | --- | --- |
| A 删闸门 | 删掉 `settle` 里 `if not isolation.complete: return Judgment(...)` | 3 集成 + 4 单元 | 3 + 4 ✅ |
| B 受阻断判定改终态 | `_record_blocked_judgment` 直接记 exhausted，且 `run_once` 计为 exhausted | 5 集成 + 7 单元 | 2 + 3（实测更严重） |
| C 隔离退回 fail-fast | `isolate_sync` 首次失败即 `break`，不再尝试其余租约 | 3 集成 + 0 单元 | 3 + 3（单元侧不变，见下） |

反例 C 值得说清：fail-fast 在集成侧红得和闸门缺失一样，因为它把 `gaps` 一起丢掉了、`complete`
变成真，于是「一条租约仍 `reserved`」与 `failed/cancellation_unconfirmed` 同时成立——正是本轮
要防的破口；单元侧 0 红是因为单测用 fake capacity，不经过逐条循环。结论一致：逐条尝试 +
complete 闸是同一件事的两半，缺一即破。

## 5. 既有断言没有被放宽

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

## 6. 必须修清单

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

## 7. 与作者自述的差异

| 作者自述 | 实测 |
| --- | --- |
| P1-① 逐条隔离、`gaps`、Job 留 `cancelling`、重启可领取、补事实自动收口 | 成立，且 fail-fast 反例证明它是必需条件 |
| P1-② 终态后置，`failed/cancellation_unconfirmed ⇒ 全部落地` | 不变量在本 head 成立；但「执行已终态」这一类被换成了永久 `cancelling`（P1-2），不是修好而是换了形态 |
| 出口：`/readyz` + diagnostics detail + worker 日志 + 钻取 SQL | 形式齐备，但 `/readyz` 在参考部署直接 503（P1-1），detail 拿不到 |
| P2：`execution_cleanup_pending` 移出 retryable、删 `Retry-After`、文档退避措辞、记账口径 | 成立，契约与生成物同步，`contracts-*` 四项 PASS |
| 新增 `LEAST(attempts+1, 64)` | 方向正确且必要，但无测试覆盖 |
| 契约审批类键精确、必须带 issue/reason、只忽略 `description` | 成立，四类攻击均被挡住 |
| 反例 3+3+4 / 3+4 / 2+3 | 我的重做：3+4 ✅、5+7（更严重）、3+0（单元侧因 fake 不经过逐条循环） |
