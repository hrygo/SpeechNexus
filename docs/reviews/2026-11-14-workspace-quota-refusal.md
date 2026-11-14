---
title: PR #732 非作者审查：无配额行 422 `workspace_not_configured`（#702）
status: active
---

# PR #732（`fix/702-quota-row`）非作者审查记录

被审对象：PR #732《fix: 无配额行不再伪装成 503，并声明默认工作区 (#702)》，
head `7cd910bc98557ed8046852dd6162e25eb50d79ae`，单 squash commit，相对其父提交
`+580 / −24`，15 文件。

审查方式：独立 worktree、**基于 `origin/main`** 拉分支（`review/733-quota-row`），被审内容用
detached 快照读取，不在被审 head 上开分支。审查只读，未改产品代码、契约或测试；全部变异在
detached 临时快照内逐次应用并 `git checkout --` 还原，未 push 任何变异。

## 元信息

| 项 | 值 |
| --- | --- |
| 被审 PR | #732 `fix/702-quota-row`，head `7cd910bc98557ed8046852dd6162e25eb50d79ae` |
| 被审 issue | #702（缺陷来源：默认部署 `workspace_quotas` 无行却回 503 `job_unavailable`） |
| PR 父提交 | `87449a69847748738a719a1f2f47edea4bc7f5a9`（`87449a69`） |
| 审查基线 | `origin/main`，审查开始时 `fe20fb99`，期间前进到 `9de12bcf`（#733 只新增一份审查文档）；报告分支已重基到 `9de12bcf` |
| 审查 worktree | `/private/tmp/sn733`（`review/733-quota-row`，基于 `origin/main`，仅含本报告） |
| 被审快照 | detached worktree `/private/tmp/sn733-pr`（`7cd910bc`） |
| 合并树 | `d9b46711a157b420d54be6ee2f484a4e7029106b` = `origin/main`（`9de12bcf`）+ PR（`git merge-tree --write-tree`） |
| 环境 | CPython 3.14.9 / uv 0.13.5 / 系统 node `v26.12.1` / pnpm 12.14.0 / PostgreSQL 18.6 独立集群 `127.0.0.1:55440` / MySQL 8.4 既有隔离容器 `127.0.0.1:55434` |
| 审查时刻 | 2026-11-14 11:23–11:47 +0800 |

## 结论

**建议改一处后合并（M1，3 行级别）；没有阻塞性安全缺陷。** 本卡主目标是「把配置缺口从
503 存储故障里拆出来」，这一点成立，且四条路径我全部独立实测可分：

1. **分码可区分（成立，实测）**：无配额行 → 422 `workspace_not_configured`（`invalid_request_error`，
   `retryable:false`，零行写入）；无 `speech_workspaces` 行 → 422 `workspace_not_found`；
   配额读取真失败 → 503 `job_unavailable`；有配额行 → 202。四个答案的状态码、错误码、
   `retryable` 与写入副作用两两不同。
2. **`_quota` 零行不得被异常路径掩盖（成立）**：`_quota` 没有 `try`，零行判断在 `execute` 之后
   独立成段；`psycopg.Error` 走 `storage_rejected` → `JobRejectedError` → 503 `job_unavailable`，
   与 422 互不掩盖（撤销准入角色 SELECT 实测 503）。
3. **白名单安全性（成立，实测）**：`_lock_submission_key` → `_existing_by_key` →
   `_replay_or_conflict` 全部早于 `build_public_job_admission`，`_quota` 位于准入事务内
   `public_job_admission.py:609`。我在**撤销 `workspace_quotas` SELECT**（配额读取必失败）的状态下
   用同一 key 重放，仍是 202 且返回原 Job，而新 key 才是 503 —— 这动态证明配额读取确实在原键重放之后，
   因此 `workspace_not_configured` 进入 `settlesReplayedIntent` 不会开「清掉未知态 → 重复执行」的口子。
4. **工作台的处置分型（不成立，M1）**：把该码登记为 `terminal` 是错的。`terminal` 的定义是
   「协议不会再接受同一请求（新意图需要新 key）」，而该拒绝在同一份请求体、**同一个 key**、
   运维补上配额行之后会成功 —— PR 自己的跨层用例与我的真实 TCP 探针都拿到 202。后果是把
   **本可重放**的持久意图作废，并且相对修复前的 503 `intent-kept` 是**退步**。

另有三项非阻塞问题（M2 契约措辞、M3 基线与合并顺序、M4 不可达分支注释），见「必须修清单」。

## 基线与方法：先说清两处 diff/数值假象

PR head 落后 `origin/main` 一个 commit，而该 commit（`fe20fb99`，#723/#724）**恰好改的是同一个
工作台文件**。因此直接对 diff 会被陈旧基线欺骗：

```text
$ git diff --stat origin/main 7cd910bc
22 files changed, 628 insertions(+), 936 deletions(-)
  .../src/file-workflow/task-journal.ts | 41 +-        # 实际是 #724 的新增被显示成「删除」
```

权威比较用三方合并树：

```text
$ git merge-tree --write-tree origin/main 7cd910bc
d9b46711a157b420d54be6ee2f484a4e7029106b
$ git diff --stat d9b46711 7cd910bc            # 反向：合并树相对 PR head 只多 #724 + 文档
$ git diff --stat origin/main d9b46711         # 正向：合并树相对 main = 本 PR 的 15 文件
 15 files changed, 580 insertions(+), 24 deletions(-)
```

即 `d9b46711` 完整保留 #724，本记录中所有「合并后」数值都在它上面取。`origin/main` 审查期间从
`fe20fb99` 前进到 `9de12bcf`，但 `git diff --stat fe20fb99 9de12bcf` 只有
`docs/reviews/2026-11-14-413-third-state.md`（+416，纯文档），后端/契约/测试零改动，
故 Python 侧数值在 `d9b46711` 上同样成立。

第二个假象是工作台用例数：作者在旧基线上测到 **833（基线 829）**，合并树实测 **862**。
差额 29 来自 #724 自己新增的工作台用例，不是本 PR 的遗漏；本 PR 的 +4 在合并树里也在
（派生：合并树 `file-task-rejection.test.ts` 含新增的 1 个 `it`、1 个 `it.each` 用例、
2 个表驱动参数）。

## 判定摘要

| # | 检查项 | 判定 |
| --- | --- | --- |
| A1 | 四条路径的码是否互不重叠且语义准确 | **成立**（真实 TCP 实测四态 + 202，零写入全部核对） |
| A2 | 多行配额歧义分支是否合理、能否构造 | **构造不出**（PK 保证最多 1 行）；属**不可达的防御代码**，分类可接受，建议加注释或删（M4） |
| A3 | `_quota` 读取异常 → 503 是否真成立、是否互相掩盖 | **成立**；补充：连接级失败/超时走 503 `job_outcome_unknown`（不是 `job_unavailable`），见 M2 |
| B4 | 新码进 `settlesReplayedIntent` 是否安全 | **安全**（静态顺序 + 撤销读取的动态重放证明） |
| B5 | 标 `terminal` 是否正确、是否作废可重放意图 | **不正确**，确实作废了本可重放的意图；相对修复前是退步 → **M1** |
| B6 | 与 #729 的 `unreplayable` 第三态是否冲突 | **语义不冲突**（两个不同象限），但**文本冲突**：#731 分支与 PR 在同一文件冲突，且 `terminal` 的分型问题在 #729 落地后更清楚 |
| C7 | 默认值只写描述、不加 `default` 关键字的归因 | **归因成立**（我加关键字复现出同一条 finding 并被拒）；但「只写描述」使默认值不可机器读取，属已声明的残差 |
| C8 | 新错误码是否走全契约流程 | **走全**：`error.code` 无枚举、生成物已重生成、drift/route-conformance/speech-changes 全绿 |
| D9 | 独立复现反例 ①/②/④ | **全部复现**：① 2 红、② 1 红、④ 4 红（与作者陈述一致） |
| D10 | 未改准入顺序（锁/写/提交边界） | **成立**：只改 `_quota` 函数体 + 两处常量/映射，无新锁、无新写、无提交边界变化 |
| D11 | 范围与 `uv.lock` | **干净**：15 文件全在 `contracts/`、生成物、`backend/`、`tests/`、`web/workbench`、`docs/` 内，`uv.lock` 未动 |
| E12 | `job_unavailable` 保持 `retryable:false` 的理由 | **理由基本成立**（该码确实被读路径复用且存在永久不可恢复的读）；但契约措辞把「连接级故障也非可重试」说满了，需收紧（M2） |

## 本卡核心一：四条路径分码是否真的可区分

真实 loopback TCP（`uvicorn.Server` + `httpx.Client`，socket 绑定 `127.0.0.1:0`），生产准入装配
`PostgresPublicJobs(admission_pool=…, require_admission=True)`，隔离 PG 18.6：

```text
PROBE transport=real-tcp base=http://127.0.0.1:53304
PROBE unnamed/no-quota      422 ('workspace_not_configured', 'invalid_request_error', False)
PROBE named/no-workspace-row 422 ('workspace_not_found', 'invalid_request_error', False)
PROBE quota-present         202 job=job-b0c16def311348caac8174a79b9440c7
PROBE replay/broken-read    202 job=job-b0c16def311348caac8174a79b9440c7
PROBE storage-read-failed   503 ('job_unavailable', 'service_unavailable_error', False)
PROBE same-key-after-prov.  202 job=job-538eafc28e6e4cbd8bb0a33031f24482 workspace=workspace-default
```

逐条核对：

| 情形 | 生产点 | status / code / retryable | 写入 |
| --- | --- | --- | --- |
| 工作区行存在、无配额行 | `_quota` 零行 → `public_job_admission.py:183` | 422 / `workspace_not_configured` / false；message `The workspace has no provisioned Job quota.` | 0 行、无计数器移动 |
| 无 `speech_workspaces` 行 | `PublicJobs._prepare_job:753-755` → `UnknownWorkspaceError` → handler | 422 / `workspace_not_found` / false | 0 行 |
| 配额读取真失败 | 撤销准入角色 `SELECT` → `psycopg.Error` → `storage_rejected` → `JobRejectedError` → `job_error("job_unavailable")` | 503 / `job_unavailable` / false | 0 行 |
| 有配额行 | 准入事务提交 | 202 + `Location`，`workspace_id` 回显 | 10 张表各 +1、`held_attempts` +1 |
| 同一 key 在补配额后重发 | 原键查找后才进准入 | 202（新 Job，`workspace-default`） | +1 |

四点补充（都影响正确性判断，不是挑刺）：

- **「无 `speech_workspaces` 行」在进入准入事务之前就被答掉**（`_prepare_job` 先读 workspace），
  所以配额 JOIN 的零行在应用层预检之后只可能是「工作区行存在、无配额行」，`workspace_not_configured`
  不会顶替 `workspace_not_found`。这条推理我核对成立。
- **「存储真不可用」并不只有 `job_unavailable` 一种形状**：`create` 的异常分支里
  `psycopg.OperationalError` / `psycopg.InterfaceError` / 事务内超时更早被
  `public_jobs_postgres.py:611-612` 捕获，返回 `outcome_unknown` → **503 `job_outcome_unknown`
  （`retryable:true`）**；只有 `psycopg.Error` 的其余子类（如本次实测的权限拒绝）才走
  `:615-616` 的 `storage_rejected` → `job_unavailable`。作者用例用「撤销 SELECT」只覆盖了后者。
  这不改变 422/503 的区分结论，但契约措辞需要按 M2 收紧。
- **零行与异常不互相掩盖**：`_quota` 全文没有 `try`；`execute` 抛出时不会走到 `if not rows`，
  零行时也不会被任何 `except` 吞掉（`_quota` 调用点在 `try:` 内只捕 `PublicJobAdmissionRejectedError`
  与 `AdmissionRejectedError`，`psycopg` 异常穿透到外层）。实测两方向都红/绿可分辨（见反例 ①②）。
- **`workspace_not_configured` 只在 create 路径出现**：`build_public_job_admission` 仅被
  `PostgresPublicJobs.create:569` 调用，`create_revision` 与取消/重试路径不经过它。

### A2：多行配额歧义——构造不出来，是防御代码

`workspace_quotas.workspace_id` 是主键（`coordination_schema.py:20-28`、
`migrations/0001_cluster_coordination_draft.sql:21-29`），`speech_workspaces.workspace_id` 也是主键
（`asset_schema.py:6-13`），JOIN 条件就是这两个主键等值，因此**最多一行**。我实测：

```text
[A2] duplicate insert WITH the PK: UniqueViolation (unreachable in production schema)
[A2] ambiguous read (PK dropped, 2 rows): code=job_unavailable
```

即：不改 schema 构造不出；把主键去掉后构造出来，`len(rows) != 1` 分支确实报 `job_unavailable`。
结论：这是**不可达的防御分支**，不是死代码缺陷。分类「读异常/歧义 → 503」在语义上可接受
（数据损坏不是调用方可修的 4xx），但建议在注释里写明「PK 下不可达」或直接改成显式不变量断言，
避免日后被误读成真实场景（M4）。

## 本卡核心二：`workspace_not_configured` 进白名单是否安全

### 静态顺序（贴行号）

`backend/src/speechnexus_backend/speech/infrastructure/public_jobs_postgres.py`（被审 head）：

```text
513  def create(self, job: NewPublicJob) -> JobCreateResult:
531          _lock_submission_key(connection, job.workspace_id, job.idempotency_key_digest)
534          existing = _existing_by_key(connection, job)
536              return _replay_or_conflict(existing, job)
569              admission = build_public_job_admission(          # ← 准入在事务内、键检查之后
589          raise _AdmissionRefusedError(error.code) from None     # ← 退出事务，回滚
597      except _AdmissionRefusedError as error:
598          return JobCreateResult("rejected", error_code=error.code)
```

`backend/src/speechnexus_backend/speech/infrastructure/public_job_admission.py`：

```text
159  def _quota(cursor: _Cursor, workspace_id: str) -> dict[str, Any]:
183          raise PublicJobAdmissionRejectedError("workspace_not_configured")   # 零行
185          raise PublicJobAdmissionRejectedError("job_unavailable")             # 多行
609      quota = _quota(cursor, job.workspace_id)                                # 准入内调用
```

`_lock_submission_key`（`:1150`）是 `pg_advisory_xact_lock`，把同一 `(workspace, key_digest)` 的
并发 keyed create 串行化到事务结束；`_existing_by_key`（`:1410`）在锁内查 `speech_jobs`。
应用层还有一道更早的同键重放（`public_jobs.py:607-616`），所以该码的产生点在**两层之后**。

### 动态证明（比静态顺序更强）

在**撤销准入角色对 `workspace_quotas` 的 SELECT**（任何配额读取都必失败）的状态下：

```text
PROBE replay/broken-read    202 job=job-b0c1…（同一 Job）
PROBE storage-read-failed   503 ('job_unavailable', …)   # 同一请求、新 key
```

同键重放仍 202，说明准入的配额读**确实没有**在原键重放之前执行；若它在前面，重放会先撞上 503。
因此：`workspace_not_configured` 只在「该 key 下没有任何 Job 行」时产生，把它加入
`settlesReplayedIntent` **不会**打开「清掉未知态 → 重复执行」的口子。**B4 判定：安全。**

一条边界：该结论依赖「键锁 — 查找 — 准入」在同一事务内且拒绝回滚；`_AdmissionRefusedError`
在 `with transaction(...)` 之外被捕获，事务整体回滚，季度 ceiling 不变。此点我用
「拒绝后零写入快照相等」实测（作者用例与我的探针都断言 `_snapshot` 前后相等）。

## 逐条判定

### A1 四条路径的码是否互不重叠且语义准确 —— 成立

见「本卡核心一」。四种情形的 `(status, code, retryable)` 两两不同，且 message 与语义匹配；
`422` 家族里 `workspace_not_found` / `workspace_disabled` / `workspace_not_configured` 各指一类
被引用资源不可用，分类与既有先例一致（`workspace_disabled` 同族）。**没有发现两个条件共用同一答案。**

### A2 多行歧义分支 —— 不可构造的防御代码

见上。分类「歧义 → 503 `job_unavailable`」合理（不可由调用方修复），但分支不可达；
`_quota` 是唯一一处「零行 4xx、多行 5xx」的读，与同文件的 `_one`（零/多行都 5xx）刻意不同，
注释已解释，可接受。

### A3 `_quota` 读取异常 → `storage_rejected` 是否真映射到 503 —— 成立，但有一处措辞过度

`_quota` 零行判断与异常路径互不掩盖（无 `try`）。异常经 `:615-616` `storage_rejected` →
`_submission_result:1516` → `JobRejectedError("storage_rejected")` → handler
`except JobRejectedError, PublicJobUnavailableError: job_error("job_unavailable")` → 503
（实测：撤销 SELECT → 503 `job_unavailable`）。**但**连接级失败（`OperationalError`/
`InterfaceError`/事务内超时）在 `:611-612` 先被捕获，得到 503 `job_outcome_unknown`，
与「读取异常 → `job_unavailable`」的单值描述不符。结论不影响本 PR 的分码，但契约/文档措辞
需要准确（M2）。

### B4 白名单安全性 —— 安全

见「本卡核心二」。静态顺序 + 动态重放双证成立；`terminal` 分支的 `settles` 还额外要求
`!this.#view.job && !this.#record?.jobId && !this.#record?.operationId &&
!contractError.detail.error.nexus?.operation_id`（`file-task-session.ts:587-593`），
即「本次会话没有已知 Job/执行身份」，进一步收紧。

### B5 `terminal` 分型 —— 不正确，会作废本可重放的意图（M1）

`file-task-session.ts:38-43` 给 `terminal` 的定义是**「协议不会再接受同一请求（新意图需要新 key）」**。
对 `workspace_not_configured` 这条为假：

- PR 自己的跨层用例在插入 `workspace_default` 配额行后，**用同一个 `Idempotency-Key:
  synthetic-unnamed-workspace`** 重发同一请求体，得到 202（`test_public_job_admission_http_pg.py`
  最后一段）。
- 我的真实 TCP 探针同样复现：`PROBE same-key-after-prov. 202 … workspace=workspace-default`。

而 `terminal` 分支会 `this.#persist({ settled: "terminal" })`（`:615`），此后
`rejected: true`、`canReplay: false`，重载后仍是「该意图已被协议确定性拒绝并作废」，
`#submit` 在 `:771` 直接返回。也就是说：**运维补上配额行（文档给出的唯一修复动作）之后，
用户手里的持久意图已经不能再用，只能显式放弃、重新选输入、换新 key。**

相对修复前这是**退步**：旧行为是 503 `job_unavailable` → `intent-kept`，用户按原键重试，
补好配额后同一意图就能成功。本 PR 让类似场景变得「原因更清楚、但出路更差」。

代码里的理由句「re-sending the same request cannot succeed」（`:77-84`）是错的；正确表述是
「在运维补上配额之前不能成功，补上之后同一 key 可以成功」。

**建议（最小改法）**：表项改 `[422, "workspace_not_configured", "intent-kept"]`，并从
`settlesReplayedIntent` 移除该码（该函数只在 `terminal` 分支被调用，移除后为死代码）。
`intent-kept` 的既有文案「提交被暂时拒绝（422 workspace_not_configured）；原意图已保留，
按原键重试不会产生第二次执行」正是该情形需要的指引；代价是此前若已有未观测尝试，
`unknown` 标记不会被清除（保守方向，安全）。
若维护者仍希望「清除旧的未知态」，则应新增第四态（如 `settled-replayable`：证明无既存 Job /
可清除 unknown、但保留可重放），而不是复用 `terminal`。属与 #729 同一张谱系，宜与 #731 一起定分型。

### B6 与 #729 `unreplayable` 是否冲突 —— 语义不冲突，文本冲突

- 语义上两者是**不同象限**：`unreplayable` =「重放按构造必然失败，但拒绝证明不了此前未执行」
  （413，答案在键查找之前）；`workspace_not_configured` =「能证明此前未执行，但重放**并非**必然失败」。
  两者互不覆盖，B6 不构成冲突。
- 分型谱系上本 PR 站错了格子：既不是 `terminal`（可重放），也不是 `unreplayable`（重放不必然失败），
  而是 `intent-kept` + 可清除 unknown。这一点在 #729 落地后更清楚。
- **文本上会冲突**：`git merge-tree --write-tree origin/fix/729-413-exit 7cd910bc` →
  `CONFLICT (content): Merge conflict in web/workbench/src/file-workflow/file-task-session.ts`
  （`rejectionTable` 与其上方文档注释区域）。两个 PR 谁先合，另一个都要 rebase 并重跑工作台用例。
  当前 `origin/main` 已合入 #731 的**审查文档**（`9de12bcf`），代码分支 `origin/fix/729-413-exit`
  （head `200cb1bb`）仍未合；建议 #731 先合、本 PR 重基后就着新分型一次改对（M3 + M1 合并处理）。

### C7 默认值只写描述 —— 归因成立，残差已声明

**归因我独立复现**：在 `JobCreateBase.workspace_id` 上加
`default: workspace-default` 关键字后：

```text
$ uv run python scripts/dev-env/check-http-contract-changes.py --base origin/main --surface speech
contracts/speech-openapi.yaml: 0.1.0.dev18 -> 0.1.0.dev18
  - contracts/speech-openapi.yaml:paths//v1/nexus/jobs/post/requestBody/application/json/oneOf:
    exclusive variants changed; compatibility needs explicit proof
Known HTTP structural checks: rejected
```

与作者描述完全一致。且既有审批表 `_APPROVED_REQUEST_BOUND_NARROWINGS` 只接受
`_BOUND_DIRECTIONS` 里的**数值收紧**（`maxLength`/`minimum`…）并校验
`_bound_movement(...) == "narrowing"`，`default` 不在其中，无法用现有审批机制放行 ——
所以「放行需改 `scripts/dev-env/check-*.py` 的审批表」这个边界判断准确。

「只写描述」是否足够：**部分足够**。生成物确实带出了默认值语义（`speech.generated.ts` 的
`JobCreateBase.workspace_id` 注释、随包发布的 `file.schema.json` description），消费者读得到；
但它是**散文**，没有机器可读的 `default`，客户端无法据此自动补字段（只能照抄字符串或依赖服务端
补齐）。作者已在提交信息与验证文档里声明该残差，属可接受的暂缓；建议另开卡补
`default` + 审批机制，而不是在审查卡里扩边界。

### C8 新错误码是否走全契约流程 —— 走全

- `error.code` 在 `contracts/shared/errors.schema.json:31-34` 只有
  `pattern: ^[a-z][a-z0-9_]{2,63}$`，**没有枚举**；新码无需改枚举，只需后端 `_ERRORS` 映射 +
  契约描述。PR 在 `job_errors.py:59` 加了 422 映射（`invalid_request_error`），
  在 `createJob` 与字段描述里列出该码。
- 生成消费者已重生成（`speech.generated.ts`、`speechnexus_contracts/file.schema.json`、
  `source-inventory.json` 的 `sha256`），`make contracts-drift` 报 `Contract bindings: up to date`。
- `check-route-conformance.py` 报 `OK: every registered speech route matches the contract`，
  无新增偏差。
- `CONTRACT_BASE_REF=origin/main make contracts-speech-changes` 通过：`0.1.0.dev18 -> 0.1.0.dev18`，
  0 条结构性 finding（本 PR 只改 description，不改结构）。**结论：流程走全，无遗漏。**

### D9 独立复现反例 —— 全部复现（见下节表格）

① 新 4xx 改回 503：2 红；② 存储真故障改新 4xx：1 红；④ 工作台归为未知：4 红。与作者陈述一致。

### D10 未改准入顺序 —— 成立

被审 diff 中后端只有三处：`_quota` 函数体（`execute`+`fetchall`+两个 raise）、
`_ADMISSION_REJECTION_CODES` 新增一个字符串、`job_errors._ERRORS` 新增一条映射。
没有新增锁、没有插入/更新语句、没有改 `admit_in_transaction` 的入参或提交/回滚边界，
`_quota` 调用点仍在原位置（`:609`，`_binding` 之后、`_instance` 之前）。这一点也由
「拒绝时行数/计数器快照完全相等」实测支持。

### D11 范围与 `uv.lock` —— 干净

15 个文件：`backend/`（3）、`contracts/speech-openapi.yaml`、生成物（3）、`tests/`（3）、
`web/workbench`（2）、`docs/`（3）。全部在本卡允许范围；`uv.lock` 未出现在 diff
（`git diff --name-only 87449a69 7cd910bc | grep uv.lock` 为空）；工作区里他人的 `uv.lock`
未提交改动我未触碰。

### E12 `job_unavailable` 保持 `retryable:false` —— 理由基本成立，措辞要收紧

作者的核心理由「同一错误码也服务读路径，存在永久过期结果，盲重试可能无限重试」**成立**：
`jobs.py` 里有 14 处 `job_error("job_unavailable", …)`，覆盖 create/list/read/cancel/retry/events
（`:515, 536, 548, 570, 591, 623, 674, 678, 698, 787-847`），其中 `_read` 的注释明确说
「A contract-invalid persisted result is unavailable storage, not a missing Job」——这种读重试
一万次也一样。`retryable` 是**按码**计算的（`job_errors.py:142-147`），因此无法只对读路径说 false、
对 create 路径说 true。

但要注意两点，避免把这条「维持现状」用过头：

1. 真正**连接级**的存储不可用在 create 路径已经走 `job_outcome_unknown`（**可重试**），
   所以维护者要求的「真 outage 标 retryable」其实**部分已被另一个码满足**；
   `job_unavailable` 承担的是「查询被拒/数据不一致」这类读侧不可恢复。
2. 因此契约新措辞「keeps its non-retryable 503 meaning for a failed or ambiguous storage read」
   说满了：连接失败的「storage read」并不落在该码上（M2）。

**判断**：保持 `retryable:false` 可接受，不必在本卡改；若要更彻底，应给 create 路径的
`job_unavailable` 做**按发射点的 retryable 覆盖**或拆码（后续卡，不在本 PR 边界）。

## 独立复现的反例（变异实验，已全部还原）

| # | 变异 | 期望 | 实际（原样输出摘要） |
| --- | --- | --- | --- |
| ① | `public_job_admission._quota` 零行改回 `job_unavailable` | 单元 + 跨层红 | **红 2 例**：`test_a_workspace_without_a_quota_row_is_workspace_not_configured`；跨层 `assert 503 == 422`，body `{"code":"job_unavailable","message":"Job storage is unavailable."}` |
| ② | `public_jobs_postgres.py:615` 分支改报 `workspace_not_configured` | 存储故障用例红 | **红 1 例**：`assert 422 == 503`，body `{"code":"workspace_not_configured","message":"The workspace has no provisioned Job quota."}` |
| ③ | 不改代码：工作区有配额行 | 仍 202 + 同键重放同 Job | **绿**：202；同键重放 202 同一 `job-b0c1…` |
| ④ | 工作台 `terminal` → `unknown-outcome`，并从白名单移除该码 | 工作台对应用例红 | **红 4 例**（exact）：`settles only 422 workspace_not_configured after the key check: true`、`422 workspace_not_configured is terminal`、`keeps a missing workspace quota distinct from a storage outage`、`settles a 422 workspace_not_configured refusal as an abandonable spent intent`；`1 failed file / 4 failed / 62 passed` |
| ⑤ | 契约加 `default: workspace-default` 关键字 | 结构检查必须拒 | **拒**：`exclusive variants changed; compatibility needs explicit proof`（C7 归因） |
| ⑥ | 多行配额歧义（A2） | 构造尝试 | PK 下 `UniqueViolation`；去掉 PK 后 `_quota` 报 `job_unavailable` |

## 实跑输出

全部在 detached 快照 `/private/tmp/sn733-pr`（`7cd910bc`）上跑；工作台用例另在合并树
`d9b46711` 上跑。PG 用独立 18.6 集群 `127.0.0.1:55440`（`initdb --locale=C` + UTF8 库），
MySQL 复用既有隔离容器 `127.0.0.1:55434`。

| 命令 | 结果 |
| --- | --- |
| `uv run pytest tests/contract -q` | **3739 passed**（154s） |
| `uv run pytest tests/integration/test_public_job_admission_http_pg.py -q` | **5 passed**（7.2s） |
| 相邻公开 Job 8 套件（atomic admission / binding pin / cancellation / list / pin http / retry sse / public_jobs_http / progress） | **96 passed**（25s） |
| `uv run pytest tests/unit/backend -q` | **3049 passed**（39s） |
| `cd web/workbench && pnpm test`（PR head） | **62 files / 833 passed** |
| `cd web/workbench && pnpm test`（合并树 `d9b46711`） | **62 files / 862 passed** |
| `make contracts-drift` | `Contract bindings: up to date` |
| `uv run python scripts/dev-env/check-route-conformance.py` | `OK: every registered speech route matches the contract; … no new deviation` |
| `CONTRACT_BASE_REF=origin/main make contracts-speech-changes` | passed，`0.1.0.dev18 -> 0.1.0.dev18`，0 finding |
| `uv run ruff check .` / `uv run ruff format --check .` | `All checks passed!` / `1838 files already formatted` |
| `uv run mypy --platform linux` | `Success: no issues found in 1449 source files` |
| `uv run lint-imports` | `Contracts: 9 kept, 0 broken` |
| `pnpm lint` | 设计 Token 通过 + `ESLint: passed` |
| `pnpm typecheck` | packages/contracts、web/shared、web/admin、web/workbench 全部 Done |
| 自写探针（已删除，不进提交） | ASGI 四路径 + 排序证明 1 passed；真实 TCP 四路径 + 同键 1 passed；A2 脚本 1 passed |

工作台用例数差异的解释见「基线与方法」：833 是作者旧基线的数，862 是合并树（含 #724 的 29 例）。

## 必须修清单

**M1（阻塞合并，3 行级别）`workspace_not_configured` 不得登记为 `terminal`。**
按 B5：`terminal` 的自有定义（「协议不会再接受同一请求」）对该码为假，PR 自身用例与我的
真实 TCP 探针都用**同一个 key** 在补配额后拿到 202；当前实现会把本可重放的持久意图作废，
且相对修复前的 503 `intent-kept` 是退步。改法二选一：
(a) 最小：表项改 `intent-kept`，同时删除 `settlesReplayedIntent` 里的该码（否则是死代码），
代码注释里「re-sending the same request cannot succeed」必须改正；
(b) 若必须清除旧的未知态：与 #731 一起增设「已结算且可重放」的第四态，不复用 `terminal`。

**M2（建议修，契约措辞）** 新描述把 503 `job_unavailable` 说成「a failed or ambiguous storage
read 都非可重试」，但连接级失败/事务内超时实际走 `job_outcome_unknown`（`retryable:true`）。
建议改成「查询被拒或数据不一致」一类限定语，避免契约把当前实现说满。同样地，
`docs/architecture/speech-api.md` 新增段落里「两者都不可重试」也需要按此校准。

**M3（机械，合并前必做）** ① 重基到 `origin/main`（当前落后 1 个 commit，且同文件被改）并重跑
工作台用例（合并树 862 例全绿，我已在 `d9b46711` 上验证）；② 与 #731（`origin/fix/729-413-exit`）
在同一文件 `rejectionTable` 区域**文本冲突**，合并顺序需明确；建议 #731 先合，本 PR 重基后按 M1
一次改对，避免两次动 `terminal` 分支。③ 文档里的工作台数字（833/基线 829）应改为合并树的
862 或注明基线。

**M4（可选，低成本）** `_quota` 的 `len(rows) != 1` 分支在 `workspace_id` 主键下不可达
（我实测：构造需要先删主键）。建议注释写明「PK 下不可达、仅防御」或替换为显式不变量断言，
防止后续读者把它当成真实场景去设计错误码。

## 新绿（独立实测，非作者提供）

- 真实 loopback TCP 四路径 + 「撤销配额读取后同键仍 202」的排序证明，1 passed。
- 合并树 `d9b46711` 上工作台 **62 files / 862 passed**（= #724 的 29 例 + 本 PR 的 4 例 +
  旧基线 829 的合并态），即本 PR 与 #724 合并后无相互回归。
- 「加 `default` 关键字必被结构检查拒绝」的归因复现（C7）。
- A2 不可达性构造实验（PK 下 `UniqueViolation`；删 PK 后 `job_unavailable`）。
- 三条反例（①②④）全部按作者陈述复现，且失败断言精确落在预期位置。

## 只能依赖作者陈述的点

- **工作台基线 829**：我只实测了 PR head 的 833 与合并树的 862，未在 `87449a69` 上重跑工作台；
  833−829=4 与 PR 测试 diff 的 +4 自洽，但「829」本身是作者陈述。
- **#731 的完整交互**：我读了 `origin/fix/729-413-exit`（`200cb1bb`）的代码与 `origin/main` 上
  已合入的审查文档，据此判定「语义不冲突」；未在 #731 分支上重跑它的用例。
- **网络分区 / 连接丢失 / failover**：作者已声明未覆盖，我也未做；「存储不可用」只用撤销
  SELECT 与代码路径分析证明，不是网络故障注入。
- **引擎注册是配额行的唯一生产写入**：我 grep 到 `INSERT INTO workspace_quotas` 只出现在
  `engine_registration.py:471`（其余为测试夹具），但未做一次真实注册的端到端验收。
- **Workbench 之外的消费者**：未验证其他客户端如何处理新码（作者也已声明）。

## 时间线（过程记录）

- `11:23` 确认仓库状态（主工作区有他人未提交的 `uv.lock`，不动；HEAD 在 `docs/tracker-batch12`）；
  `git fetch`，`origin/main = fe20fb99`；读 PR head 提交信息与 diff。
- `11:24` 建独立 worktree `/private/tmp/sn733`（`review/733-quota-row`，基于 `origin/main`）与
  detached 快照 `/private/tmp/sn733-pr`（`7cd910bc`）；`git diff origin/main --stat` 为空已核实。
- `11:25` 起隔离 PG 18.6（55440，独立 data dir，UTF8 库）；`uv sync --locked --all-packages
  --all-groups`、`pnpm install --frozen-lockfile`（331ms，复用 store）。
- `11:27` 跑准入 HTTP 集成 5 passed；随后搭 ASGI 探针（四路径 + 撤销读取的同键重放证明）。
- `11:28`–`11:35` 跑 `tests/contract`（3739）、`tests/unit/backend`（3049）、workbench（833）、
  mypy、ruff、lint-imports、`pnpm lint` / `pnpm typecheck`，全绿。
- `11:36` 跑 `contracts-drift`、`check-route-conformance`、`contracts-speech-changes`（base `origin/main`）。
- `11:37` 依次做变异 ①、②、④，每条 `git checkout --` 还原并确认 `git status` 干净；
  另复现 ④ 的 4 个失败用例名。
- `11:38` 真实 TCP 探针（`uvicorn.Server` + loopback socket）复跑四路径 + 同键补配额后 202。
- `11:39` A2 多行实验（PK 下 `UniqueViolation`；删 PK 后 `job_unavailable`）。
- `11:40` 契约 `default` 关键字实验 → 复现 `exclusive variants changed` → 还原。
- `11:42` 发现 `origin/main` 期间前进到 `9de12bcf`（#733 只加审查文档）；核 `fe20fb99..9de12bcf`
  仅 1 个文档文件；在合并树 `d9b46711` 上重跑工作台（62 files / 862 passed）；把报告分支重基到
  `9de12bcf`。
- `11:45` 跑相邻公开 Job 8 套件（96 passed），确认无副作用回归。
- `11:47` 写本报告，清理临时探针文件，`ruff format` 后提交。

## 审查边界

- 只读审查：未修改产品代码、契约、测试；变异全部在 `/private/tmp` 临时快照内完成并 `git checkout --`
  还原，未 push 任何变异；报告分支基于 `origin/main`，`git diff origin/main --stat` 只含本文件。
- 未验证范围见「只能依赖作者陈述的点」。
- 本记录的实测数值对应「`origin/main`（`9de12bcf`）+ PR `7cd910bc`」的合并树
  `d9b46711a157b420d54be6ee2f484a4e7029106b`；工作台 833 与 862 两个数字分别对应 PR head 与合并树，
  已在正文注明。
