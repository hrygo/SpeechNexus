# PR #825 非作者审查：制品保留期写入与清理（#820）

- **被审对象**：PR #825 `fix/820-artifact-retention`，head `6ca635d51ce4edf0abe70ffbb2a38e6f7a5cd13c`，35 文件 +1778/−58
- **审查分支**：`review/827-artifact-retention`，基于 `origin/main`（`1c3d09d3`）新建
- **审查时间**：2026-11-15（Asia/Shanghai）
- **说明**：`docs/reviews/` 目录在本仓库不存在（`ls docs/reviews` → `No such file or directory`），本文件是该目录的首个文件，未覆盖任何既有审查记录。
- **隔离环境**：本机 PostgreSQL 18.6 独立库 `sn827_it` + 独立容器 `sn827-minio`（`127.0.0.1:29100`，版本化桶），审查结束后已删除（见文末「过程记录」）。

---

## 结论

**不通过（Reject / Request changes）。** 核心设计成立，A1 崩溃窗口与 A2 两层防线都经独立实测通过；但本 PR 当前**CI 红**，且存在一个会让参考部署清理进程**完全起不来**的装配缺陷，以及一个把「发布重放」从幂等变成永久不可结算的语义回归。

- ✅ **成立的部分**：`expires_at_us` 落库、三种响应区分、逐段复查期限谓词、数据库时钟判定、`UPDATE (artifact_state)` 最小授权、四段崩溃窗口重跑全部收敛（实测）。
- ❌ **必须修（4 项）**：
  1. CI 红：`uv run mypy` 5 处错误（全部在本 PR 新增/修改的测试文件）。
  2. CI 红：`tests/integration/test_voice_drafts_pg.py` 3 条既有用例未随 `require_ready=False` 语义更新；`origin/main` 同用例 9 passed，PR head 3 failed。
  3. 参考部署 `artifact-retention` profile 以 `SPEECHNEXUS_API_DATABASE_URL`（`sn_api`）运行会被启动权限检查**拒绝**，profile 起不来（实测复现）。
  4. 保留期届满后同一 Job 的发布重放抛 `DispatchProtocolError`，Job 永久停在 `running`/`unknown`；最小合法窗口（1 小时）即可触发。
- ⚠️ **建议修（5 项，见末节）**：`content_already_removed` 命名与语义不符、无持久删除账本时刻、溢出分支误用可重试异常、三种响应在工作台无消费者、`mvp-release-readiness.md` 与实测冲突。

---

## 逐条判定

### A1 崩溃窗口与幂等 —— ✅ 通过（实测）

清理一次 `sweep()` 分四段：`expired()`（只读选择）→ `mark_expired()`（UPDATE ready→expired）→ `delete_version()`（撤对象版本，`stat` 确认不存在）→ `mark_deleted()`（UPDATE expired→deleted）。逐段列窗口与重跑结果，全部在真实 PG + 版本化 MinIO 上实测（脚本 `harness.py`，见附 1）：

| 窗口 | 崩在哪 | 重跑行为（实测） | 判定 |
| --- | --- | --- | --- |
| W0 | `expired()` 选择（`SET TRANSACTION READ ONLY`） | 无状态变更，重跑重新选择 | ✅ 幂等 |
| W1 | 标了 `expired`，还没撤对象 | `_expire_pass` 不再选中它（只选 `ready`），`_removal_pass` 选中并完成删除 → `deleted` | ✅ 正确继续 |
| W2 | **对象已撤、行还没标 `deleted`** | `remove_object` 对不存在的版本幂等；随后 `stat` 得 `NoSuchVersion` → 视为成功 → `mark_deleted` → `deleted` | ✅ 正确识别「本来就不在」 |
| W3 | 撤对象失败（`stat` 无法确认） | 抛 `artifact_cleanup_unconfirmed` → 行保留 `expired`、字节仍在；下一轮重试 → `deleted` | ✅ 正确重试 |
| W4 | 已标 `deleted` | 两条选择语句都不选 `deleted`，`sweep()` 返回 `()` | ✅ 终态幂等 |

实测输出（真实 PG 18.6 + 版本化 MinIO）：

```text
== A1 crash windows ==
W1 expired-then-crash  decisions= [('artifact-w1', 'deleted', '')]
   state= deleted  bytes-present= False
W2 object-gone-then-crash decisions= [('artifact-w2', 'deleted', '')]  pre-bytes-present= False
   state= deleted
W3 unconfirmed-removal decisions= [('artifact-w3', 'blocked', 'content_remove_unconfirmed')]
   state= expired  bytes-present= True
W3 retry decisions= [('artifact-w3', 'deleted', '')]  state= deleted
W4 idempotent-second-pass decisions= []
```

**关键判定**：W2 是任务点名担心的那一段，实际实现是**正确**的——`MinioArtifactRemoval.delete_version` 只在 `stat` 报 `NoSuchKey/NoSuchObject/NoSuchVersion` 时 `return`（成功），其余一律 `artifact_cleanup_unconfirmed`。因此「对象不在」既不会被误判成「保留期内被误删」，也不会被当成失败而永久卡住。

**残留提示（非崩溃，属命名问题，见「建议修 1」）**：`mark_deleted()` 返回 `False`（转移未生效）被映射为 `reason="content_already_removed"`。该字符串只表示「守卫 UPDATE 没有命中」，覆盖「被并发清理抢走」「期限未到」「状态已变」三种情况，日志复核时会误导。

### A2 两层防线是否真的独立 —— ✅ 通过（实测，且有边界说明）

实测方式：只把 SQL 选择谓词 `_ELAPSED` 放宽成 `TRUE`（运行期 monkeypatch 模块常量 `artifacts_postgres._ELAPSED`，不落盘、不改仓库文件），保留行内固定的 `expires_at_us` 与应用层 `ArtifactRecord.retention_elapsed(now_us)` 复核：

```text
== A2 two independent layers ==
A2 widened-SQL decisions= [('artifact-elapsed', 'expired', ''), ('artifact-retained', 'blocked', 'retention_not_elapsed'), ('artifact-elapsed', 'deleted', '')]
   retained state= ready  bytes-present= True
   elapsed state= deleted  bytes-present= False
```

- 保留期内的 `artifact-retained` 被 SQL **选中**，但应用层复核拦下（`retention_not_elapsed`），状态保持 `ready`，**对象字节仍在（`bytes-present= True`）**。
- 同一次 pass 里到期行正常走完 `expired → deleted`。
- 同一变异下 PR 自己的集成用例变红（见附 2）：`assert {('artifact-real-retained-1','blocked'), …} == {('artifact-real-elapsed-1','expired'), …}`。

**判定：两层防线确实独立。** 应用层复核不依赖 SQL 的过滤结果——它读的是同一语句返回的 `now_us` 列（数据库时钟）并自行比较 `expires_at_us`。放宽 SQL 不会删掉保留期内对象的字节。

**但要说清边界**：PR 的集成用例在放宽 SQL 后是**先**在 `decisions` 断言处变红，字节断言在其后，所以**该用例本身无法观察「字节是否保住」**；「字节保住」这一结论由本次独立脚本给出，PR 自述「应用层复核仍保住保留期内对象的字节」是正确但**在自己的测试里不可见**的。建议后续把「保留行状态与对象版本仍在」的断言前移，或单列一条变异可观察的用例。

### A3 `expires_at_us` 的时钟来源 —— ⚠️ 应用时钟 + 数据库时钟混合

- `created_at_us` 来源是**应用时钟**：`ResultArtifactPublisher._publish` / `_publish_validation` 里 `created_at_us = self._now_us()`，默认实现是 `lambda: time.time_ns() // 1_000`（`results.py`）。测试 `test_publication_records_the_configured_retention_deadline` 用固定的 `now_us=lambda: 1_700_000_000_000_000` 证明落库的 `created_at_us` 就是这个注入值。
- `expires_at_us = created_at_us + retention_us`，因此**期限原点也是应用时钟**。
- 但**判定**用数据库时钟：`PostgresArtifactRetention` 的 `_NOW_US = CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT)`，`PostgresArtifactMetadata.get` 也返回同一个表达式。

**后果**：设 δ = 发布时刻的应用钟 − 同一时刻的数据库钟。行内固化的是 `expires_at_us =（应用钟时刻）+ 窗口`，判定却按数据库钟推进，因此从真实时间轴看，实际保留期 = 配置窗口 − δ：δ > 0（发布主机超前）会提前到期，δ < 0（发布主机落后）会推迟到期。毫秒级 NTP 偏移可忽略；若某主机时钟错到小时/天级，保留期会相应缩短或延长。跨主机部署时这是**真实存在**的偏差来源，但方向只是「按记录值提前/推后到期」，不会绕过行内固定的 `expires_at_us`。

**判定**：不是缺陷，但**必须写进文档**（当前 `docs/architecture/runtime-governance.md` 只写「按数据库时钟判定」，未说明期限原点来自应用时钟、偏移影响的窗口长度）。这也解释了为什么 `created_at_us` 与 DB 的 `CHECK (expires_at_us > created_at_us)` 永远成立——两者都由应用提供。

顺带核实：`PostgresArtifactRetention._read` 在**无行**时回退到 `self._now_us()`（应用时钟），该值不参与任何决策（空批次不产生 outcome），无影响。

### B4 三种响应是否泄漏信息 —— ✅ 对内部工作台可接受（理由已落到文档）

实测判定链（`ArtifactService._load`）：

1. 行存在（**本 workspace**，`get()` 内联 `speech_workspaces.workspace_state='active'` 与 workspace 等值连接）→ `200` 元数据 / `410 artifact_expired` 或 `410 artifact_deleted`。
2. 行不存在且 id 落在派发器铸造空间 `artifact-[0-9a-f]{48}` → `404 artifact_not_published`。
3. 行不存在且 id 不在该空间 → `404 artifact_not_found`。

**泄漏面**：
- **跨 workspace 不泄漏**：`_load` 先按 workspace 过滤，他 workspace 的行与「完全不存在」得到同一答案 `artifact_not_published`。这是正确的隔离方向。
- **本 workspace 内**：知道 id 的人可以区分「有过 committed 行」与「从未有行」。由于 `artifact_id = "artifact-" + sha256("public-job-result:{job_id}:{kind}")[:48]`，而 `job_id` 是该 API 的公开资源，**id 可被推导**——所以这条区分对「已知 job」的人不构成新增秘密，增量仅是「该 job 的结果制品是否已提交」，响应体不含内容、对象键、版本或时间线。
- `404 artifact_not_found` 只在「非铸造形状」出现，语义与「本 API 从未签发」一致，不指向任何具体行。

**判定：对内部工作台可接受。** 理由：泄漏的是「本 workspace 内某个可推导 id 的提交事实」，不跨 workspace、不含内容、不暴露 staging（`_load` 明确不探测 staging 与对象）；而收益是 PRD 要求的三种事实可分辨（保留期届满 / 清理已移除 / 未发布）。**已落在下一个人能看到的地方**：`docs/architecture/speech-api.md`（路由表与 410 错误码表）与 `deploy/README.md` 的 `Artifact retention` 一节都写明了四种答案的区分。建议补一句「该区分是本 workspace 内的存在性事实，不构成跨 workspace 探测」到 `speech-api.md` 的 404 行，避免后来人误以为它只是文案差异。

### B5 `retryable` 是否一致 —— ⚠️ 契约层正确，工作台无消费者（证据见附 3）

- 后端：`artifact_error()` 里 `retryable = code in ("artifact_service_unavailable", "artifact_storage_unavailable", "artifact_not_published")`。`artifact_not_published` = 发布可能尚未提交 → 同一请求以后可能成功，标 `true` 合理；`artifact_expired` / `artifact_deleted` / `artifact_not_found` 为终态，`false`。
- 消费者核对：
  - 唯一真正读 `error.retryable` 的工作台路径是 `web/workbench/src/recovery/command-journal.ts` 的 `refusedCategory(settled, retryable)`（`settled` 对 4xx（除 408/429）为真）。按该口径，`404 artifact_not_published` + `retryable: true` → 记为 `retryable` → 允许重放。**语义一致。**
  - 但**制品下载路径不读 `retryable`**：`web/workbench/src/books/RenderUnitPanel.tsx:233` 是 `if (error.status === 404 || error.status === 410) return "missing"`，按 **HTTP 状态**分类；`web/workbench/src/manuscripts/editor-session.ts:470` 用 `[400,404,409,410,…].includes(error.status)` 判定 settled。也就是说 404/410 三个新码在工作台**全部折叠成同一个「missing」**，`retryable: true` 无人读取。

**判定**：`retryable` 取值本身与工作台既有口径不冲突（没有任何消费者会把它读错），但**新的三态在当前工作台看不到**。这不是本 PR 的错误（前端不在其范围），但 PR 声称「让过期下载可复核」，端到端只做到了 API 层。要么本 PR 补一条工作台消费（把 404/410 细分），要么在报告/issue 里显式记为剩余项。

### C6 `ge=3600 / le=10y` 与 `CHECK (expires_at_us > created_at_us)` 的交互 —— ✅ 通过

- 下限 3600 s > 0，`expires_at_us = created_at_us + 3600e6 > created_at_us` 恒成立，**最小值足以保证约束不被违反**。
- 上限 10 年 = `MAX_RESULT_RETENTION_US`（`results.py`），`JobDispatchSettings.result_retention_seconds` 的 `le=10*365*24*60*60` 与之一致，`ResultArtifactPublisher.__init__` 再校验 `1 <= retention_us <= MAX_RESULT_RETENTION_US`。
- 溢出覆盖：`_expires_at()` 检查 `expires_at_us > 2**63 - 1` → 抛 `DispatchUnavailableError`。按当前时钟（`created_at_us ≈ 1.77e15`）+ 最大窗口（`3.15e14`）总和 ≈ `2.09e15`，距 `2**63-1 ≈ 9.22e18` 与 `public_jobs._TIMESTAMP = 2.53e17` 都有数量级余量，**该分支实际不可达**。
- 入口覆盖：`expires_at_us` 的唯一写入点是 `PostgresArtifactWriter.insert_ready`，唯一生产者是 `ResultArtifactPublisher` 两条分支（普通结果与 voice_validation 报告），两者都过 `_expires_at`；域层 `ArtifactRecord` 还有 `expires_at_us > created_at_us` 与 `JobArtifactFact.expires_at_us <= _TIMESTAMP` 两道。**全部入口覆盖。**

**两处小问题（建议修 3、4）**：`_expires_at` 的溢出分支抛的是 `DispatchUnavailableError`（可重试的「存储暂不可用」），配置错误被当作瞬时故障会被无限重试；另外域层 `ArtifactRecord` 没有 `expires_at_us` 上界，DB 也没有 `CHECK (expires_at_us <= …)`，边界只靠调用方。

### C7 参考部署 profile 与 `artifact_retention_grants` —— ❌ 权限最小且足够，但**身份装配使其必然启动失败**

**权限本身**：
- `artifact_retention_grants` = `USAGE` + `SELECT` + `UPDATE (artifact_state)`。实际所需正是这三项：`_read` 需要 `SELECT`；`mark_expired` / `mark_deleted` 只写 `artifact_state`；对象删除用对象存储凭据，不用 DB 权限。**最小且足够。**
- `UPDATE` 只给 `artifact_state` 一列**没有问题**：清理不写任何其它列（期限与创建时刻刻意保留供事后复核），`artifact_state` 的取值由表级 `CHECK (artifact_state IN ('ready','expired','deleted'))` 约束。启动检查还额外拒绝「任何其它列的 UPDATE」。

**身份装配（实测失败）**：`deploy/compose.yaml` 的 `artifact-retention` 服务用 `SPEECHNEXUS_DATABASE_URL: ${SPEECHNEXUS_API_DATABASE_URL}`，即 `sn_api`；`ArtifactRetentionRuntime._verify_retention_authority` 里的 `refuse_foreign_relation_privileges(...)` 会拒绝**角色在 schema 内任何未声明表上的权限**。而 `sn_api` 在参考部署里持有 `coordination_runtime_grants(schema, speech_role="sn_api", …)` 与 `asset_runtime_grants(schema, "sn_api")`，覆盖大量其它表。实测（附 4 的 `reference_identity_faithful`，用仓库自带的两个 grant 函数原样装配）：

```text
== C7 faithful reference identity ==
C7 reference identity also holds table privileges on: ['admission_attempts', 'admission_leases',
 'admission_operations', 'admission_requests', 'coordination_jobs', 'coordination_outbox',
 'engine_instances', 'execution_stop_evidence', ...]
C7 faithful reference identity -> refused: ValueError Artifact retention startup rejected (ValueError); verify artifact_retention_grants and the role
```

对照组（只装 `artifact_retention_grants` 的专用角色）在同一环境下 `-> started`。

**结论**：参考部署按当前 compose/env 与 runbook 装配后，`artifact-retention` profile **启动即退出**，一条保留期都不会清理。这同时是一条**文档与实测冲突**：`deploy/README.md` 与 `docs/architecture/runtime-governance.md` 都写「以 API 运行身份 + `artifact_retention_grants` 运行，启动拒绝更宽的写权限」，但没有说该检查还会拒绝「schema 内其它表的任何权限」，而 `sn_api` 必然持有它们。

**修法（推荐第 1 种）**：
1. 给清理进程独立身份：新增 `SPEECHNEXUS_ARTIFACT_RETENTION_DATABASE_URL`（与既有 `artifact_reader_database_url` 同型）+ 迁移/runbook 装一个只持有 `artifact_retention_grants` 的 `sn_artifact_retention` 角色。这样「最小权限 + 启动自证」同时成立。
2. 或把 `refuse_foreign_relation_privileges` 的检查面收窄到 `speech_artifacts`（会削弱该检查的既有价值，需单独评审）。

**本 PR 的测试覆盖缺口**：`test_retention_role_may_not_widen_into_content_deletion` 只验证「多加表级 UPDATE 被拒」，**没有**验证「与非专用身份共用时会被拒」——所以这条装配缺陷不会被现有用例发现。

### D8 断言强度（复现反例）—— ✅ 三条自报反例全部复现，且都能红

用脚本按锚点替换源码、跑指定用例、随后恢复文件（变异只在临时 worktree，未 push）：

| # | 变异 | 用例 | 结果（附 2） |
| --- | --- | --- | --- |
| ① | `ArtifactRecord.retention_elapsed` → `return False` | `tests/unit/backend/test_artifact_http.py` | 红：`assert 200 == 410`（`test_expired_deleted_and_unreadable_content_are_distinguishable`） |
| ② | 发布分支 `expires_at_us = None` | `tests/unit/backend/test_result_publication.py` | 红：`assert None is not None`（`test_publication_records_the_configured_retention_deadline`） |
| ③ | 删掉 `_removal_pass` 里的期限复核 | `tests/unit/backend/test_artifact_retention.py` | 红：决定从 `retention_not_elapsed` 变成 `content_already_removed`（`test_a_row_marked_expired_inside_its_window_is_not_deleted`） |
| ④ | （追加）SQL 谓词放宽为 `TRUE` | `tests/integration/test_artifact_retention_pg.py` | 红：保留期内的行被选入 `decisions`（见 A2） |

① 与 ② 的实测输出：

```text
>           assert reply.status_code == 410
E           assert 200 == 410
FAILED tests/unit/backend/test_artifact_http.py::test_expired_deleted_and_unreadable_content_are_distinguishable
1 failed, 4 passed

>           assert committed.expires_at_us is not None
E           AssertionError: assert None is not None
FAILED tests/unit/backend/test_result_publication.py::test_publication_records_the_configured_retention_deadline
1 failed, 1 passed
```

③ 的实测输出（决定性片段）：

```text
E       AssertionError: assert (RetentionOut...dy_removed'),) == (RetentionOut...ot_elapsed'),)
E         At index 0 diff: … reason='content_already_removed') != … reason='retention_not_elapsed')
```

**判定**：断言强度足够——三类失败模式（判定丢失、发布不写期限、应用层复核丢失）都有能变红的用例，且失败信息点名具体契约。

### D9 `_published()` 要求 `status == "ready"`：30 天后重放会不会炸 —— ❌ 会炸，是真实语义回归

**实测**（附 5，用 PR 自己的发布分支与测试 fake，只把已提交行改成清理后会留下的状态）：

```text
replay with row state=ready    -> OK artifact-3136840d…
replay with row state=expired  -> DispatchProtocolError: Committed Artifact belongs to another result
replay with row state=deleted  -> DispatchProtocolError: Committed Artifact belongs to another result
```

**后果链**（代码阅读，`speech/application/results.py` + `speech/application/job_dispatcher.py`）：

1. `ResultArtifactPublisher._publish` 先 `self._rows.find(artifact_id)`，命中即走 `_published()`；`_published` / `_published_validation` 要求 `record.status == "ready"`，否则 `DispatchProtocolError`。
2. 调用点 `job_dispatcher._complete`（`job_dispatcher.py:422`）→ 异常被外层 `except DispatchProtocolError` 捕获，且此时 `phase.gateway` 为真 → 返回 `DispatchResult(job_id, "unknown", "outcome_unknown")`。
3. Job 因此**永远不进入 `succeeded`**：每条后续轮询都重复同一条路径，直到 Job 自己的期限后按 `unknown/job_timeout` 结束或悬挂。已提交的 Artifact 行与 receipt 都在，但 Job 的终态无法结算。

**可达窗口**：
- 直接触发点：`publish` 已提交行、`_finish(…,"succeeded")` 之前进程崩溃 → Job 留 `running` → 下一个 worker 重新领到并重跑 `_complete` → 重放。
- 触发时点：重放发生在 `expires_at_us` 之后。默认 30 天需要「Job 卡住 30 天」，不现实；但 `SPEECHNEXUS_ARTIFACT_RETENTION_SECONDS` 的**合法最小值是 3600（1 小时）**，此时「发布后崩、调度器停机或反复重启超过 1 小时」就是现实场景。
- 注意 `_follow` 里 `state == "succeeded"` 的分支在 `self._expired(job)` 判定**之前**，所以 Job 期限不会兜住这条路径。

**判定：不可接受，不是「过期后不可重放」的合理设计。** 理由：
1. 它**违背本 PR 自己写的原则**。`JobArtifactFact` 的 docstring 与 PRD §F-JOB 都主张「succeeded Job 保留终态，保留期届满只把 Artifact 标为过期」——而「发布已提交、Job 待结算」正是这条原则应当覆盖的场景，`_published` 却要求 `ready`。
2. 它把**幂等重放**（`insert_ready` 的 `ON CONFLICT DO NOTHING` + `find` 快路径，docstring 明写「an unchanged replay included」）变成了**永久不可结算**，且失败是静默的（Job 停在 `running`，对外只有 `unknown`）。
3. 不是 PR 前就有的行为：`origin/main` 上 `expires_at_us` 恒 `None`，行永远不会离开 `ready`，重放永远成功。**这是本 PR 引入的新失败模式。**

**修法**：`_published` / `_published_validation` 的身份校验（workspace、kind、media_type、receipt_id 一致）足以证明「已提交的是这次发布」，把 `record.status != "ready"` 从「拒绝」改成「接受」——`ready` 只应决定**能否提供内容**（下载路由已经独立判定 `artifact_expired` / `artifact_deleted`），不应决定**Job 能否结算**。

### E10 范围 —— ✅ 只在预期内，无越界改动

按类别核对 35 个文件：

- 发布路径：`results.py`、`job_dispatch_runtime.py`、`job_dispatch.py`（新增铸造空间自检）、`settings.py`
- 下载路径：`application/artifacts.py`、`domain/artifacts.py`、`interfaces/http/artifact_errors.py`
- 清理入口：`application/artifact_retention.py`、`artifact_retention_worker.py`、`artifact_retention_runtime.py`、`infrastructure/artifacts_postgres.py`、`infrastructure/artifacts_minio.py`
- 授权：`infrastructure/artifact_schema.py`、`compose-minimal.md`
- 配置：`environment.py`、`deploy/compose.yaml`、`deploy/compose.env.example`、`pyproject.toml`（入口点）
- Job 读取：`public_jobs.py`（application/domain/infrastructure 三处）
- 测试 8 个、文档 5 个（`deploy/README.md`、`runtime-governance.md`、`speech-api.md`、`compose-minimal.md`、`plans/mvp-release-readiness.md`）

**没有**顺手改发布原子性：`put_verified` 先写对象、`insert_ready` 后提交行的顺序与 `ON CONFLICT DO NOTHING` 语义未动。**没有**改下载鉴权模型：`PostgresArtifactMetadata.get` 的 workspace 等值与 `workspace_state='active'` 谓词未动，改动只在「行不存在时的回答」与「不可服务时的错误码」。**没有**手改生成产物：`contracts/`、`packages/contracts/`、生成客户端均未出现在 diff 中。

唯一需要注意的**消费者可见语义变化**不在文件范围里，而在行为上：Job 读取在 Artifact 过期后由 `503` 变成 `200`（并列出该 Artifact）。这正是下面 E11 的 CI 失败来源。

### E11 例数与 `gh pr checks 825` —— ⚠️ 例数属实，但 CI 全红（8 fail / 16 pass / 1 skipping）

例数复核（在 PR head 的独立 worktree 上实测）：

| 范围 | 自报 | 实测 | 结论 |
| --- | --- | --- | --- |
| `tests/unit/backend` | 3243（基线 3228） | `3243 passed in 54.38s` | ✅ 一致 |
| `tests/contract` | 3813 | 总计 3813（`8 failed, 3805 passed`；8 条全是 `typescript` 用例，属本机缺 node 生成包的环境限制，无一涉及 artifacts） | ✅ 计数一致 |
| `tests/ci` | 370 | `370 passed, 816 subtests passed` | ✅ 一致 |
| 集成（真实 PG + 版本化 MinIO） | 3 条 | `tests/integration/test_artifact_retention_pg.py` + `test_artifact_http_real_storage.py` → `3 passed` | ✅ 一致 |

`gh pr checks 825`（2026-11-15 10:5x 拉取）：

```text
fail  Python: lint, types and contract compatibility
fail  Python: tests, coverage and package build
fail  Integration 1: PG/MySQL/MinIO
fail  Integration 2: PG/MySQL/MinIO
fail  Integration 4: Web + PG/MySQL/MinIO
fail  Gate: Python execution and static checks
fail  Gate: required checks for this change
fail  Gate: six-shard integration evidence and merged coverage
pass  Compose: configuration validation / scope / Deploy dry-run / build & probe 等
```

**两类根因，都不是环境抖动：**

1. `Python: lint, types…` 的 step `Check Python types with mypy` 失败。CI 日志与本机复现一致：

```text
tests/integration/test_artifact_retention_pg.py:330: error: Value of type "dict[str, object] | None" is not indexable  [index]
tests/unit/backend/test_public_job_service_reads.py:172: error: Call to untyped function (unknown) in typed context  [no-untyped-call]
tests/unit/backend/test_public_job_service_reads.py:174: error: …  [no-untyped-call]
tests/unit/backend/test_public_job_service_reads.py:177: error: …  [no-untyped-call]
tests/unit/backend/test_artifact_retention.py:218: error: Argument 2 to "ArtifactService" has incompatible type "ServiceObjects"; expected "ArtifactObjects"  [arg-type]
Found 5 errors in 3 files (checked 1464 source files)
```

5 处全部落在本 PR 新增/修改的测试文件里，属作者自测未覆盖 `mypy`。

2. `tests/integration/test_voice_drafts_pg.py` 3 条既有用例被本 PR 的语义变更打红（本机真实 PG 复现，且用 `origin/main` 做对照）：

```text
# PR head (6ca635d5)
FAILED tests/integration/test_voice_drafts_pg.py::test_candidate_requires_a_matching_receipt_and_ready_artifact[not-ready-receipt-synthetic-receipt-synthetic-expired-True]
FAILED tests/integration/test_voice_drafts_pg.py::test_validation_report_readback_requires_matching_ready_artifact[validation-receipt-negative-expired]
FAILED tests/integration/test_voice_drafts_pg.py::test_preview_readback_requires_a_matching_ready_artifact[preview-result-expired-receipt-preview-result-True]
3 failed, 6 passed, 67 deselected in 2.47s

# origin/main (1c3d09d3) 同一批用例
9 passed, 67 deselected in 9.28s
```

变异定位（把 `public_jobs.py` 里 3 处 `require_ready=False` 临时改回 `True`）：`9 passed` → 确认三条失败**完全由** `require_ready=False` 读投影变更引起。该文件在 PR 中零改动（`git diff origin/main...6ca635d5 -- tests/integration/test_voice_drafts_pg.py` 为空），作者更新了 `test_voice_generation_dispatch_pg.py`、`test_voice_validation_dispatch_pg.py`，漏了这一份。

**附带语义问题（需在修测试时一并定调）**：`require_ready=False` 只该影响 **Job 读取**（succeeded Job 保留终态，PRD §F-JOB 有据）。但它同时让**voice draft 候选视图**在预览 Artifact 过期后仍报 `status=candidate` 并列出 `preview_artifact_ids`，而准入/写入路径 `_require_candidate` 仍用默认 `require_ready=True` → 同一对象「读得到候选、改不了」的不一致。要么 draft 视图保持 503（并把 `require_ready=False` 收窄到 Job 读取），要么明确接受并同步更新写入侧与文档。

---

## 核心三问

### 1. 崩溃窗口与幂等

四段窗口全部实测收敛，**没有必须修的段**：

- 标 `expired` 未撤对象 → 重跑继续（W1）。
- **对象已撤、行未标 `deleted`** → 重跑靠 `stat` 的 `NoSuchVersion` 识别「本来就不在」并落 `deleted`，不会误判为保留期内误删（W2）。这是本卡最担心的一段，实现正确。
- 撤对象未确认 → 行保持 `expired`、字节保留、下一轮重试（W3）。
- 终态 `deleted` → 不再被任何语句选中（W4）。

唯一需要陪跑整改的是**日志语义**：`mark_deleted()` 失败被记成 `content_already_removed`，会把「并发抢占 / 期限未到 / 状态已变」说成「内容已被移除」，复核日志的人会被误导。

### 2. 两层防线是否独立

**独立，且实测有效。** SQL 谓词放宽成 `TRUE` 后，保留期内的行被选中但被应用层 `retention_elapsed` 复核拦下（`retention_not_elapsed`），状态保持 `ready`、对象字节保留；同一 pass 里到期行正常删除。应用层复核读的是同一语句返回的数据库 `now_us`，不依赖 SQL 过滤结果，因此放宽查询不能提前删内容。

**证据强度要打折的地方**：PR 自己的集成用例在放宽 SQL 后**先**在 `decisions` 断言处失败，字节断言在其后执行不到，所以「字节保住」这一结论只能由独立脚本给出（本报告已给出）；建议把字节/状态断言前移，让这条防线在自己的用例里可观察。

### 3. 30 天后重放会不会炸

**会炸，且不是「30 天后」才炸——合法最小窗口 1 小时即可炸。** `_published` 要求行仍为 `ready`；行被清理标成 `expired` 后，同一 Job 的发布重放抛 `DispatchProtocolError`，`job_dispatcher._complete` 把它变成 `unknown/outcome_unknown`，Job 永远不进入 `succeeded`。`origin/main` 上不会有这个问题（`expires_at_us` 恒 `None`，行永不离开 `ready`），所以是本 PR 引入的语义回归，并且与本 PR 自己主张的「succeeded 终态不受保留期影响」矛盾。修法：`_published` / `_published_validation` 只校验身份与 receipt，不再要求 `ready`。

---

## 必须修清单

| # | 项 | 证据 | 修法 |
| --- | --- | --- | --- |
| 1 | `uv run mypy` 5 处错误，CI `Python: lint, types…` 红 | 附 6；CI job `132674135855` step 18 | 修 `test_artifact_retention_pg.py:330`、`test_public_job_service_reads.py:172/174/177`、`test_artifact_retention.py:218`（给 `ServiceObjects` 补 `open` 签名/`AbstractContextManager` 返回类型，`cast` 掉 `dict \| None`，标注未类型化 helper） |
| 2 | `tests/integration/test_voice_drafts_pg.py` 3 条既有用例未随 `require_ready=False` 更新，CI 3 个集成 shard 红 | 附 7（PR head vs `origin/main` 对照 + 变异定位） | 按最终定调更新这 3 条（job 读取 → 200 并列事实；draft 候选视图 → 见下方语义决议），并明确 `require_ready=False` 只作用于 Job 读取还是也作用于 draft 视图 |
| 3 | `artifact-retention` profile 以 `SPEECHNEXUS_API_DATABASE_URL` 运行会被启动检查拒绝，参考部署起不来 | 附 4（`refused: ValueError Artifact retention startup rejected`） | 给清理进程独立身份（`SPEECHNEXUS_ARTIFACT_RETENTION_DATABASE_URL` + 只持有 `artifact_retention_grants` 的角色），并同步 `deploy/compose.yaml`、`deploy/README.md`、`docs/operations/compose-minimal.md`、`runtime-governance.md`；补一条「共用广权限身份被拒绝」的集成用例 |
| 4 | 保留期届满后的发布重放抛 `DispatchProtocolError`，Job 永久不可结算 | 附 5 | `_published` / `_published_validation` 去掉 `status != "ready"` 的拒绝，只保留身份/kind/media/receipt 校验；补一条「行已 `expired` 仍能结算 succeeded」的用例 |

---

## 建议修（不阻塞合并但应跟进）

1. **`content_already_removed` 命名**：`mark_deleted()` 返回 `False` 的真实含义是「本次转移未命中」，建议改为 `transition_not_applied` 之类，避免日志复核误读。
2. **缺持久删除账本时刻**：行内只保留 `created_at_us` / `expires_at_us`，「何时移除」只在进程 JSON 日志里；仓库「删除与恢复」原则要求删除有持久账本。建议加 `deleted_at_us`（用数据库时钟写），代价极小。
3. **溢出分支误用可重试异常**：`_expires_at` 越界抛 `DispatchUnavailableError`（= 存储暂不可用，会被无限重试），应改为配置类错误或 `ValueError`。
4. **三种响应在工作台无消费者**：`RenderUnitPanel` 把 404/410 一律归 `missing`，`retryable: true` 无人读取。记入后续（或在 PR 描述中显式声明剩余项）。
5. **`mvp-release-readiness.md` 与实测冲突**：B5 已改成「已实现」，但参考部署该 profile 起不来（必须修 3）。修完 3 之后再更新该行，或明确写「代码已实现、参考部署未验证」。
6. **`ArtifactRecord` 缺 `expires_at_us` 上界 / DB 无上界 CHECK**：当前只靠调用方，建议域层与 DDL 各补一道。

---

## 只能依赖作者陈述的点（本次未独立验证）

1. **`SPEECHNEXUS_ARTIFACT_RETENTION_SECONDS` 默认 30 天对应 PRD F-ARTIFACT**：本次核对了 `docs/prd.md` 与 `runtime-governance.md §2.3` 的表述存在该承诺，但未逐字复核 PRD 原文对「普通生成音频 30 天」的措辞强度。
2. **`checks`/`coverage` 门禁的细分口径**：只核到「job 失败在哪一步」与失败断言，未逐条复核 merged-coverage 聚合算法的正确性。
3. **`tests/contract` 3813 中 8 条 `typescript` 失败的性质**：本机缺 node 生成包/构建产物导致，未在 CI 侧逐条确认其失败原因与本地一致（CI 的 contract 相关 step 均通过）。
4. **真实参考部署上 `artifact-retention` 的运行时行为**：本次用「仓库自带 grant 函数 + 真实 PG/MinIO」复现启动拒绝，未在完整 Compose 项目里起该 profile 观察退出码与日志形态。
5. **工作台真实浏览器侧对三种新响应的渲染**：只做了静态代码核对（`RenderUnitPanel` / `editor-session` / `command-journal`），未跑 Chromium 用例。
6. **MySQL/InnoDB 侧**：本卡的清理路径只在 PostgreSQL 上（`artifact_retention_grants` 是 PG 授权声明），未验证 InnoDB 侧的等价性（PR 也声称无需迁移）。

---

## 过程记录（时间线）

- **10:41** 复核工作区状态，确认 `docs/reviews/` 不存在（`ls docs/reviews` → No such file），故 `docs/reviews/2026-11-15-artifact-retention.md` 为新建文件，未覆盖任何既有记录。
- **10:42** `git fetch` + `git worktree add /private/tmp/sn827 -b review/827-artifact-retention origin/main`（base `1c3d09d3`），另建临时 worktree `/private/tmp/sn827-head`（detached `6ca635d5`）用于运行与变异，两者写作用域分离。
- **10:43** 读 `.agents/skills/speechnexus-deliver-slice/SKILL.md`、`docs/operations/production-coordination-storage.md`（145 行，确认本 PR 未改协调事务，租约/fencing 约定不受影响）。
- **10:44–10:50** 用 `git show 6ca635d5:<path>` 逐文件读被审内容（application/infrastructure/domain/worker/runtime/compose/docs/tests），并用 `git diff origin/main...6ca635d5` 建立范围基线。
- **10:47** `gh pr checks 825` 首轮：`Python: lint, types and contract compatibility` 已 fail，集成与 tests 仍 pending。
- **10:49** 起隔离环境：`CREATE DATABASE sn827_it`；`docker run … -p 127.0.0.1:29100:9000 sn827-minio`（首个端口 19000 被本机 `Workbuddy` 占用，改 29100）。
- **10:50** 建 `/private/tmp/sn827-head` 并 `uv sync --all-packages --frozen`。
- **10:51** `tests/unit/backend` → `3243 passed in 54.38s`（与自报一致）。
- **10:52** `uv run mypy` → 5 errors，与 CI 一致。`tests/ci` → `370 passed, 816 subtests`。
- **10:53** 真实 PG + 版本化 MinIO 跑 `test_artifact_retention_pg.py` → `2 passed`。
- **10:55** 自写 `harness.py`（仓库外）测 A1 四段崩溃窗口、A2 SQL 放宽 + 应用层复核、C7 身份装配；全部得到 A1/A2 通过、C7 拒绝的结论。
- **10:58** `tests/contract` → `8 failed, 3805 passed`（8 条均为 `typescript` 环境型失败）。
- **11:00** 复现作者自报反例 ①②③ 与 SQL 放宽的集成红（脚本按锚点替换后立即恢复，`git status` 干净）。
- **11:02** `gh pr checks 825` 复采：8 项 fail / 16 pass / 1 skipping（含 3 个集成 shard 与 3 个聚合 Gate）。拉 CI 日志定位到两类根因（mypy 5 处 / `test_voice_drafts_pg.py` 3 条）。
- **11:05** 本机真实 PG 复现 `test_voice_drafts_pg.py` 3 条失败；在 `origin/main` worktree 上跑同批用例得 `9 passed`；再用「3 处 `require_ready=False` 改回 `True`」变异定位到唯一根因。
- **11:08** 写本报告；`uv run --no-project ruff format --check` 通过（分支仅有本 markdown 文件）。
- **11:10** 清理：`DROP DATABASE sn827_it`、`docker rm -f sn827-minio`、移除两个临时 worktree（保留 `review/827-artifact-retention` 分支与其提交）。

---

## 附录：证据脚本与命令

所有证据脚本放在仓库外 `/private/tmp/sn827-evidence/`，不进入提交。

**附 1 `harness.py`（A1/A2/C7）** 关键命令：

```bash
cd /private/tmp/sn827-head   # detached 6ca635d5
export SPEECHNEXUS_TEST_DATABASE_URL="postgresql://<user>@127.0.0.1:5432/sn827_it" \
       SPEECHNEXUS_TEST_MINIO_ENDPOINT="http://127.0.0.1:29100" \
       SPEECHNEXUS_TEST_MINIO_ACCESS_KEY=… SPEECHNEXUS_TEST_MINIO_SECRET_KEY=…
uv run --no-sync python /private/tmp/sn827-evidence/harness.py /private/tmp/sn827-head all
```

**附 2 `mutate.py`（D8 反例）** 按锚点替换 → 跑单条用例 → 立即恢复：

```bash
uv run --no-sync python /private/tmp/sn827-evidence/mutate.py no-expiry-decision
uv run --no-sync python /private/tmp/sn827-evidence/mutate.py publish-writes-none
uv run --no-sync python /private/tmp/sn827-evidence/mutate.py no-removal-recheck
uv run --no-sync python /private/tmp/sn827-evidence/mutate.py widen-sql   # 需 MinIO 环境变量
```

**附 3（B5）** 消费者核对：

```bash
grep -rn "retryable\|410\|404" web/workbench/src/books/RenderUnitPanel.tsx \
  web/workbench/src/manuscripts/editor-session.ts \
  web/workbench/src/file-workflow/file-task-session.ts
grep -rn "refusedCategory" -A 4 web/workbench/src/recovery/command-journal.ts
```

**附 4（C7）** `reference_identity` / `reference_identity_faithful`：用仓库自带 `coordination_runtime_grants(schema, speech_role="sn_api", gateway_role="sn_gateway")` + `artifact_retention_grants(schema, role)` 原样装配后 `ArtifactRetentionRuntime.open()`，得到 `→ refused`；对照组只装 `artifact_retention_grants` 得到 `→ started`。

**附 5 `replay_after_retention.py`（D9）**：

```bash
uv run --no-sync python /private/tmp/sn827-evidence/replay_after_retention.py
# replay with row state=ready    -> OK
# replay with row state=expired  -> DispatchProtocolError: Committed Artifact belongs to another result
# replay with row state=deleted  -> DispatchProtocolError: Committed Artifact belongs to another result
```

**附 6（must-fix 1）**：

```bash
cd /private/tmp/sn827-head && uv run mypy | tail -2
# Found 5 errors in 3 files (checked 1464 source files)
```

**附 7（must-fix 2）**：

```bash
# PR head
SPEECHNEXUS_TEST_DATABASE_URL=… uv run --no-sync pytest tests/integration/test_voice_drafts_pg.py -q -k ready_artifact
# 3 failed, 6 passed, 67 deselected
# origin/main worktree
cd /private/tmp/sn827 && SPEECHNEXUS_TEST_DATABASE_URL=… uv run --no-sync pytest tests/integration/test_voice_drafts_pg.py -q -k ready_artifact
# 9 passed, 67 deselected
```
