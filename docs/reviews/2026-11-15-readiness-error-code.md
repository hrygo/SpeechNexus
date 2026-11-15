---
title: "PR #839 非作者审查：readiness 过期错误码与续期路径（#828）"
status: active
version: "0.1.0"
date: 2026-11-15
---

# PR #839 非作者审查：readiness 过期错误码与续期路径（#828）

审查对象：PR [#839](https://github.com/hrygo/SpeechNexus/pull/839)
`fix/828-readiness-expiry`，head `956b43d426a840b750f405a043a325950089df15`，
base `origin/main` = `c672b6b2`（分支 merge-base = `5cd5a5d0`，即 #833 之后、#838/#841 之前）。
被审内容 32 文件、+2032/−41。审查分支 `review/843-readiness-error-code`（本报告是它与
`origin/main` 的唯一差异）。

## 结论

**方向正确，两方向区分实测成立，消费者覆盖对仓库内是完整的，生成产物未被手改；但存在四条
必须修的问题，其中两条属本卡核心。**

1. 新码确实「可区分且可重试」，过期与真实存储故障在真实 PG + 生产装配下用同一请求体分出两个
   码，我独立复现了作者报的全部三段响应（§C）。
2. **「只修了这一个」而不是「把这一类都修了」**：`rejection_code()` 仍把 **7 个**非容量码折叠成
   `job_unavailable`，其中 `reconciliation_required`、`policy_or_resource_disabled`、
   `version_conflict`、`stale_fencing_token`、`recipe_mismatch` 与 #828 同属「操作员介入或
   普通重试后同一请求可成功」类。我把收据的 `fencing_token` 改一位（身份漂移，
   `reconciliation_required`）后实测仍是 `503 job_unavailable` + `"Job storage is unavailable."`
   + `retryable: false`——**运维看到的假存储故障在同类条件下仍可复现**（§A1、§C9）。
3. **只在 description 里声明的错误码消费者无法机器发现**：`ErrorBody.code` 是 pattern
   （`^[a-z][a-z0-9_]{2,63}$`），生成的 TS 是 `code: string`、Python 是 `code: str`，没有任何
   枚举或码表。而 `contracts/speech-openapi.yaml` 自身已有 `allOf: [ErrorResponse] + code: const`
   的逐响应收窄机制（`:2687-2692`）可用却未用。**结论：不够；应进逐响应枚举，但不应改共享
   `errors.schema.json` 的 `code`**（§A2）。
4. **`retryable: true` 与同一契约里最近的先例相反**：`workspace_not_configured`（运维补配额后同一
   key 可放行）被刻意定为 `retryable: false`；`engine_not_ready` 是同一「跨请求外的运维动作才能
   恢复」形状却定成 `true`，且不带 `Retry-After`。按契约自己的 `retryable` 定义（"Whether the
   same dispatch may be retried **after the refusal**"），立即重试不会成功，因此该字段与
   `description` 里"retrying before the receipt is re-signed cannot succeed"自相矛盾（§A4）。

补充判定：

- **`/readyz` 有意不因过期失败——取舍正确**，但补偿不完整：参考部署里**没有任何自动信号**
  （报告不进 healthcheck、Compose 不调度、启动闸门只在启动时打印一次），收据过期后
  `/readyz` 仍 `200` 而所有创建 `503`，与 #840「健康检查看不见」同类。文档已声明需要自建
  cron/systemd，但这属于「声明了残余风险」，不是「消除了风险」（§B8）。
- 反例强度足够：四条反例我都独立复现真红（含真实 PG 上两条）(§C9)。
- 例数全部与我的实测一致，且作者未定位的「基线差值」可以定位到 **#841（`fb8f8fa8`）在
  merge-base 之后新增 1 个 `tests/ci` 用例**，与本 PR 无关（§D13）。

## 环境

| 项 | 值 |
| --- | --- |
| 主机 / 时间 | macOS arm64，2026-11-15（Asia/Shanghai），16:30 起 |
| 审查 worktree | `/private/tmp/sn843`，分支 `review/843-readiness-error-code`（基于 `origin/main` = `c672b6b2`） |
| 被审代码 worktree | `/private/tmp/sn843-run`，detached 于 `956b43d4`（只读上下文；变异只在此副本内，逐次 `git checkout --` 复原） |
| Python / pytest / pnpm | 3.14.9 / 9.1.1 / 12.14.0 |
| PostgreSQL | 一次性容器 `sn843-pg`（`postgres:16.15-alpine`，`127.0.0.1:55443`），审查结束 `docker rm -f` |
| MySQL | 一次性容器 `sn843-mysql`（`mysql:8.4.8`，`127.0.0.1:55444`），审查结束 `docker rm -f` |
| DSN | `SPEECHNEXUS_TEST_DATABASE_URL`、`SPEECHNEXUS_TEST_MYSQL_URL` |

作者的 PG 容器是 `sn838-pg`（55460），MySQL 用的是仓库既有 `speechnexus-coordination-mysql`。
我另起两个一次性容器，未触碰既有实例与共享 GPU。

## 时间线（过程记录）

| 时刻 | 动作 | 结果 |
| --- | --- | --- |
| 16:30 | 建 `sn843`（基于 `origin/main`）与 `sn843-run`（detached `956b43d4`）；确认 `docs/reviews/2026-11-15-readiness-error-code.md` 不存在 | 文件不存在，可新建 |
| 16:31 | 读 `contracts/AGENTS.md`、`speechnexus-contract-change/SKILL.md`、`docs/operations/async-job-runbook.md` | 前置完成 |
| 16:32 | 读 `rejection_code()` / `job_error()` / `require_valid()` / `_ADMISSION_REJECTION_CODES` 全链 | 定位折叠点与消费者入口 |
| 16:33 | 采集 §D 例数（PR head 与 `origin/main` 干净检出各一次） | 3294/7154/3825/408 与 3273/7133/3813/399 |
| 16:34 | `gh pr checks 839` | 26 项 pass，1 项 skipping，0 fail |
| 16:35–16:38 | 起 PG/MySQL；跑 `tests/unit/backend`+`tests/unit/test_admission.py` | `3348 passed` |
| 16:39 | 起 probe 打印两方向真实响应（临时改测试加 print，跑完 `git checkout`） | `engine_not_ready` / `job_unavailable` 两段原文 |
| 16:40 | `generate-contract-bindings.py --check`（等价 `make contracts-drift`） | `Contract bindings: up to date`，exit 0 |
| 16:41–16:44 | 反例 1–4（逐条应用→跑→`git checkout --`） | 四条全红，两条在真实 PG 上红 |
| 16:45 | 只读角色跑 `speechnexus-engine-readiness`（B6/B7） | 退出码 0/5、`expiring`/`expired`、表不变 |
| 16:46 | `--renew-readiness` 在收据仍 valid 时连跑两次（B5） | `renewed=True`，`verified_at_us` 严格递增 |
| 16:47 | 身份漂移（`fencing_token+1`）后创建（A1 同类余量实证） | 仍 `503 job_unavailable` + "Job storage is unavailable." |
| 16:48 | `tests/unit tests/contract tests/ci` 全量 | `11384 passed, 3 skipped, 1357 subtests` |
| 16:49 | 删 probe、清 worktree、`DROP` 容器 | 无残留 |

## A. 契约变更与消费者

### A1 新码是否「可区分且可重试」，以及还有哪些码被折叠

`rejection_code()`（`backend/src/speechnexus_backend/speech/infrastructure/public_job_admission.py:986-996`）
现行规则：先 `_CAPACITY_CODES` → `job_capacity_exhausted`，再 `_PUBLIC_CODES.get(code, "job_unavailable")`，
其中 `_PUBLIC_CODES = {"idempotency_conflict": ...（恒等）, "readiness_expired": "engine_not_ready"}`。
`engine_not_ready` 的映射点放在容量判断之后是正确的：`quota_exceeded`/`capacity_exhausted`
仍优先落到 429，不会被新码吞并。`require_live_authority` 是唯一入口（`admission_sql.py:243`、
`:344`、`decide_admission` 内的 `:368` 都经同一 `AdmitRecipe` 事务端口），所以 `readiness_expired`
只有一条到达 `rejection_code()` 的路径。

**被折叠成 `job_unavailable` 的全部非容量码（7 个）与逐个判断：**

| # | 准入码 | 触发条件（代码位置） | 折叠结果 | 与 #828 同类？ | 判断 |
| --- | --- | --- | --- | --- | --- |
| 1 | `reconciliation_required` | 收据描述另一个实例/generation/topology/qualification；收据 `verified_at_us` 在未来（时钟不一致）；`ready_at_us` 与收据不符；池 owner 状态非 active | `job_unavailable`（非重试） | **是**（重签/重注册后同一请求可成功，除非 generation 真的不对） | **未修**。实测仍报 "Job storage is unavailable." |
| 2 | `policy_or_resource_disabled` | 实例 `instance_state != ready`/`base_budget_kind != steady`/节点非 active（`admission.py:427`）；或 recipe 审批窗口 `admission_expires_at_us` 已过（`:314`）、池 `pool_state != active`（`:324`） | `job_unavailable`（非重试） | **是**（重注册/重建实例后同一请求可成功；审批窗口过期只需重新批准） | **未修**。这是同一缺陷类里影响面最大的一项：实例侧不可用不是存储故障 |
| 3 | `recipe_mismatch` | 请求与已批准 recipe 的 binding/manifest/digest/policy 不符（`:365`、`:412`） | `job_unavailable`（非重试） | **是**（重注册到匹配的 plan 后同一请求可成功） | **未修** |
| 4 | `version_conflict` | 配额/实例/主机版本 CAS 失败（`:375`、`:414`）；attempt 0 内部重试一次，attempt 1 仍冲突则折叠 | `job_unavailable`（非重试） | **是**（纯并发噪声，再发一次通常成功） | **未修**，且方向是错的：这是最典型的「重试即可」却被标成不可重试 |
| 5 | `stale_fencing_token` | 池 owner 租约过期/换主（`owners.py:216`，经 `require_authority`） | `job_unavailable`（非重试） | **是**（owner 续租/接管后同一请求可成功） | **未修** |
| 6 | `lease_not_active` | 请求自己的 `deadline_at_us` 已过（`:312`） | `job_unavailable`（非重试） | **是**（新请求即可成功） | **未修** |
| 7 | `invalid_counter` | 时钟/计数器不合法（`:310` 前置、`:462`、`owners.py:209`） | `job_unavailable`（非重试） | 否 | 折叠可接受（属内部不变量破坏），但 `"Job storage is unavailable."` 文案仍是错误归因；严格说该是 500 类 |

**结论：这是「只修了这一个」，不是「把这一类都修了」。** 作者并非不知情：新契约测试
`tests/contract/test_public_job_engine_readiness.py::test_only_the_named_admission_causes_leave_the_unavailable_fallback`
把这些折叠**逐条 pin 住**（`reconciliation_required` / `recipe_mismatch` / `stale_fencing_token`
→ `job_unavailable`），说明是有意识的收窄而非遗漏。但由此产生两个后果必须交代：

- #828 的**用户可观察症状**（健康存储被报成 "Job storage is unavailable." 且 `retryable:false`）
  在上述 6 类条件仍然完全复现。我的实证：把收据的 `fencing_token` 加 1（除收据窗口外一切不变），
  创建回答

  ```text
  503 {"error": {"code": "job_unavailable", "message": "Job storage is unavailable.",
       "param": null, "request_id": "e45829115b8243119d68df65b48bf4da",
       "retryable": false, "type": "service_unavailable_error"}}
  ```

  这正是 #828 报的那个响应，只是原因换了。
- `_CAPACITY_CODES`/`_PUBLIC_CODES` 之外没有「为什么这个码不配自己的答案」的机器可读依据，
  只有注释。下一个加码的人不会知道这条边界在哪。

### A2 契约声明是否完整：只在 description 声明够不够

**不够：消费者无法机器发现这个码。** 依据：

1. `contracts/shared/errors.schema.json` 的 `ErrorBody.code` 是
   `{"type": "string", "pattern": "^[a-z][a-z0-9_]{2,63}$"}`，**不是枚举**；整个 schema 的
   `enum` 只出现在 `ErrorType` 与两个 `allOf/if` 的 `operation_in_progress`/
   `response_not_replayable` 条件里。任何符合 pattern 的字符串都通过校验。
2. 生成的消费者产物里也没有码表：`packages/contracts/src/speech.generated.ts:2563` 是
   `code: string;`，`packages/contracts/src/speechnexus_contracts/models.py:102` 是 `code: str`。
   一个 TS/Python 消费者**无法**从契约包知道 `engine_not_ready` 存在，只能读
   `description` 的自然语言或读后端实现 `_ERRORS`。
3. 本仓库**自己已有**逐响应枚举机制：`contracts/speech-openapi.yaml:2687-2692` 对某 503 用
   `allOf: [$ref ErrorResponse] + error.properties.code.const` 把码变成机器可读的 const。
   `createJob` 的 503 仍是 `$ref: "#/components/responses/ServiceUnavailable"`（`:944-945`），
   所以新码只留在描述里。
4. 内部协议本来就是枚举的：`contracts/internal/execution-control.openapi.yaml` 的
   `ExecutionErrorCode` 是 enum，生成的 `execution_models.py:111` 是 `Literal[...]`。
   即**内部协议的错误码词表是机器可读的，公开 Job 创建面不是**——这与本仓库
   「`#473` 只有 `error.code` 能区分」的既有结论方向相反。

**建议（不必改共享 schema）**：给 `createJob` 的 503 加逐响应收窄，
`enum: [job_unavailable, job_outcome_unknown, job_admission_contended, engine_not_ready]`
（不能写 `const` —— 该状态被四个码共用），必要时同时给 429/422 收窄。共享
`errors.schema.json` 的 `code` 保持 pattern 是对的：它跨 speech/realtime/admin/workbench
四个协议，逐操作词表本就不该塞进共享定义。

### A3 消费者同步是否真的完整

我按「谁 POST `/v1/nexus/jobs`」与「谁按 `job_unavailable`/5xx 分支」两条线逐个核到代码：

| 位置 | 角色 | 结论 |
| --- | --- | --- |
| `web/workbench/src/file-workflow/speech-file-client.ts:92` | 仓库内**唯一**的 `createJob` 调用点 | 拒绝表在 `file-task-session.ts`，已加 `[503, engine_not_ready, intent-kept]` ✓ |
| `web/workbench/src/file-workflow/file-task-session.ts:89-102` | `rejectionDisposition()` 的 `(status, code)` 精确表 + 兜底 | 兜底 `unlisted 5xx → unknown-outcome` 会被新码命中，加表后正确避开；`settlesReplayedIntent` 未加（503 不结算意图，与 `job_unavailable` 一致）✓ |
| `web/admin/src/task-center/job-client.ts` | 只有 `list` / `getJob` / `cancel` / `events`，**没有 create** | 无需改。`cancel` 不经 readiness 判定，不会发新码 ✓ |
| `web/admin/src/task-center/TaskCenter.tsx:94` | 把 `detail.error.code` 原样渲染 | 通用，不硬编码 `job_unavailable` ✓ |
| `web/workbench/src/voices/*`（voice_drafts 消费者） | 走 voice_draft 错误码命名空间 | `voice_drafts.py` 把 `engine_not_ready` 归入 retryable 的 `unavailable` 类（→ `voice_draft_unavailable`），新码**不会**以 `engine_not_ready` 出现在这个面 ✓ |
| `packages/contracts`（SDK/契约包） | 只生成类型，无重试逻辑，`code: string` | 无分支可同步；但见 A2——它也因此无法枚举新码 ✗（声明性缺口，非代码缺口） |
| `execution-gateway` / `node-controller` / `asset-inspector` / `scripts/` | 不消费公开 jobs 面（`grep job_unavailable` 仅命中 `check-async-job-prereqs.py:832` 的一句**提示文案**，且那句讲的是 DSN 为空） | 无需改 ✓ |
| `deploy/job-startup-gate.py` | 启动闸门 `pending` 行 | 已同时点名续期与报告 ✓ |

**结论：作者列的清单（workbench 两个文件 + `voice_drafts.py`）对仓库内是完整的，没有漏掉的
生产分支。** 我额外核出的只有两类**非阻塞**问题：

- `file-task-session.ts:59-62` 的注释现在**事实错误**：它写「错误表把 `job_outcome_unknown`、
  `job_capacity_exhausted`、`job_admission_contended` **exactly** 这三个标为 retryable」，
  实际已是四个（含新码）；同一段引用的行号 `job_errors.py:129-134` 与 `:113` 也失效
  （基线就已是 143-147/127，现在 158-163/127）。这是本 PR 改的同一段注释块，顺手更新即可。
- 同一注释说「客户端完全不消费 `error.retryable`」——我复核成立：`file-workflow` 全链只在注释里
  出现 `retryable`；真正读该字段的是 `recovery/command-journal.ts:61` 的
  `refusedCategory(settled, retryable)`，但它的入参是 `WorkbenchResponseError.retryable`
  （workbench 面），与 speech Job 无关。

### A4 破坏性影响分析是否准确

作者原话：把新码折回 `job_unavailable` 的旧客户端仍安全（保持非重试），只是丢失恢复线索。

**前半句成立，后半句不成立。**

- **成立**：仓库内 consumption 确实不读 `error.retryable`（A3 已核），`file-task-session`
  按 `(status, code)` 精确表分类；旧客户端若把新码当未知 5xx，会落进 `unknown-outcome`
  而非 `intent-kept`——比现在**更保守**，不会重复执行。因此「折回安全」在仓库内是真的。
- **不成立**：`retryable: false → true` 不是「只丢线索」。契约明写
  「callers must read `retryable` rather than infer it from the status」
  （`contracts/speech-openapi.yaml:840-841`），所以任何按契约实现的外部 SDK / 网关
  都会**因为这一位改变行为**：从「不重试、等人工」变成「可重试」。而这个码要成功**必须**先有
  一次带外运维动作（重签收据），立即重试永远失败，且该响应**不带 `Retry-After`**
  （只有 `job_capacity_exhausted` 带，`job_errors.py:165-169`）。一个指数退避的重试器会在
  收据被重签之前持续放大创建流量，最长可达一个 TTL（默认 24 h）。
- 更关键的是**同一契约内的先例相反**：`workspace_not_configured` 的形状与本码完全一致
  （「retrying the same request cannot succeed beforehand, while the same request under its
  original idempotency key is admitted afterwards」），却被刻意定为
  `retryable: false`（`contracts/speech-openapi.yaml:832-839`、`job_errors.py:55-65`）。
  本 PR 让形状相同的两种拒绝给出相反的 `retryable`，且没有说明为什么。

**因此「旧客户端仍安全」只在「不读该字段」的前提下成立；契约层面它是行为变更，应当按
破坏性变更说明**，或者把 `retryable` 改成 `false`（与 `workspace_not_configured` 一致），
或者保留 `true` 但补 `Retry-After`（把「什么时候重试才有意义」变成可执行的提示）。

## B. 续期机制

### B5 `--renew-readiness` 是否真能在收据仍有效时重签

**是，实测成立。** 在真实 PG 上连续跑三次注册（TTL 86400）：

```text
[PLAIN REGISTER ] outcome=registered    state=valid renewed=False verified=1794732638889481
[--RENEW (valid) ] outcome=already_ready state=valid renewed=True  verified=1794732638930502
[--RENEW (again) ] outcome=already_ready state=valid renewed=True  verified=1794732638949956
[MONOTONIC      ] verified strictly increases: True
```

实现是 `engine_registration._existing_result()` 的
`renewed = readiness.expires_at_us <= now_us or renew_readiness`：过期**总是**重签，
未过期待 `--renew-readiness`。同时 `engine_instances.ready_at_us` 在同一事务里被写成本次探测
时刻（准入要求 `verified_at_us <= ready_at_us <= now`），所以这不是「只改文档」的假续期。
反例（去掉 `or renew_readiness`）会同时打红真实时钟集成用例与事务单测——见 §C9 第 2 条。

### B6 只读报告是否真的不写库、不探测

**是，读代码 + 只读角色实跑都成立。**

- 代码：`engine_readiness._queried_rows()` 只做两件事——`connection.read_only = True`，然后
  `SELECT clock_timestamp()` 与一条 `JOIN` 查询；模块不 import 任何 HTTP 客户端，不读
  `SPEECHNEXUS_ENGINE_HEALTH_URL`，不调用 `register_engine`。写权限在数据库侧被强制
  （`read_only` 会开 `READ ONLY` 事务）。
- 只读角色实跑：给新角色只 `GRANT USAGE ON SCHEMA` + 三张表的 `SELECT`，用它跑 CLI：

  ```text
  [ROLE CHECK] UPDATE refused: InsufficientPrivilege        ← 角色确实是只读的
  [REPORT exit] 0
  [REPORT stdout] {"expires_at_us": 1794732586475684, "instance_generation": 1,
    "instance_id": "instance-reg-1", "readiness_state": "expiring", "remaining_seconds": 1,
    "remediation": "run speechnexus-register-engine --renew-readiness before expires_at_us",
    "target_node_id": "node-reg-1"}
  {"summary": {"expired": 0, "expiring": 1, "invalid": 0, "missing": 0,
    "ready_instances": 1, "unusable": 0, "valid": 0}}
  [REPORT stderr] <empty>
  [NO WRITE] tables unchanged: True
  ```

  退出码 0；`engine_instances` / `runtime_instance_heads` / `runtime_instance_intents` 三张表
  前后快照完全一致。

### B7 退出码 0/5、`--fail-on-expiring`、以及「只靠这条命令能否发现即将过期」

- 真实时钟走过 2 s TTL 后：`readiness_state: "expired"`、`remaining_seconds: -1`、
  stderr `error: readiness_expired: instance-reg-1 g1 (...)`、**退出码 5**
  （`EXIT_UNAVAILABLE`，`engine_registration.py:63`）。`missing`/`invalid` 也返回 5。
- `--fail-on-expiring` 让 `expiring` 也返回 5；单测
  `test_main_can_treat_an_expiring_receipt_as_an_alert` 覆盖。（我探针里那一步在收据已过期后
  才加该开关，退出码 5 来自 `expired` 而不是 `expiring`，所以这一条我采信单测 + 代码，不把
  自己的探针当证据。）
- **「运维是否只靠这条命令就能发现即将过期」：不能，仍须自建调度。** 命令是一次性 CLI
  （`engine-readiness` profile 是 `restart: "no"` 的 one-shot，Compose 不调度），它只在被调用时
  报告。文档把这条写清楚了：`deploy/README.md`「Compose does not schedule it: call it from the
  host timer, e.g. hourly with cron or a systemd timer」、`async-job-runbook.md`
  「Compose 不负责调度，须用宿主机 cron 或 systemd timer 以短于 TTL 的间隔调用续期」、
  `compose.yaml` 的 profile 注释同样写了。**所以可执行性要求已被声明，但没有任何默认装配**：
  一套照文档起起来的参考部署，在运维不加定时器时仍然「无人发现」。这是 §B8 的同一个缺口。
- 一处小出入：`docs/operations/register-engine.md` 写 `remaining_seconds`「已过期为负」，
  而 `now_us == expires_at_us` 这一瞬 `expiry_state()` 已判 `expired` 但
  `remaining_us // 1_000_000 == 0`（单测 `test_the_report_separates_valid_expiring_and_expired_against_one_clock`
  里 `report[2]["remaining_seconds"] == 0` 就是这种行）。文案与边界行为差 1。

### B8 `/readyz` 有意不因过期失败——取舍对不对，以及会不会是另一个「健康检查看不见」

**取舍本身对，但补偿不完整，属于 #840 那一类风险未闭合。**

- 取舍正确：`/readyz`（`backend/src/speechnexus_backend/speech/interfaces/http/health.py:54`）
  只做数据库 `SELECT 1` 与对象存储健康 URL。收据过期时**读取、取消、事件流全部仍然可用**，
  让 `/readyz` 503 会把整个 API 摘出负载均衡，把一个「只能创建」的降级放大成全面不可用。
  这是正确的失败域划分。
- 补偿不完整：参考部署里现在**没有任何自动信号**能看见它——
  `api` 的 healthcheck 是 `/readyz`（仍 200）；`speechnexus-engine-readiness` 没有
  healthcheck、不被 Compose 调度；`deploy/job-startup-gate.py` 只在 worker 启动时打印一次
  `pending`（且是 `pending` 不是 `fail`）。所以「收据过期 → 所有创建 503 而一切健康检查绿灯」
  是可复现状态，与 #840（`/healthz` 200 但页面白屏）同构。
- 已声明的缓解：`async-job-runbook.md §2` 早就写了「通过 `/readyz` 也不代表 `/v1/nexus/jobs`
  可用」，本 PR 又补了 profile、报告与续期步骤。**但这仍是「文档要求运维自建 cron」，
  不是「系统保证有人会看到」。**
- 建议（非阻塞，但值得单开一张卡）：让「创建可准入」成为一个**独立的、可被探针轮询**的面，
  而不是要求运维自己拼定时器——例如在 `/readyz` 的 `checks[]` 里增加一条
  `job_admission`（`status: degraded`，**不**改变 200），或提供一个
  `GET /v1/nexus/jobs/admission-readiness` 汇总面。这样既不动 `/readyz` 的失败域，
  也不依赖人的自觉。

## C. 反例与断言强度

### C9 独立复现的反例（四条，逐条应用后 `git checkout --` 复原）

**反例 1 —— `rejection_code()` 恢复折叠（#828 原状）**：`"readiness_expired": "engine_not_ready"`
→ `"job_unavailable"`。

- 契约层：`1 failed, 41 passed`，`assert 'job_unavailable' == 'engine_not_ready'`
- 真实 PG + 生产装配 HTTP 层：

  ```text
  assert ('job_unavail...error', False) == ('engine_not_..._error', True)
  At index 0 diff: 'job_unavailable' != 'engine_not_ready'
  FAILED tests/integration/test_public_job_admission_http_pg.py::test_an_elapsed_readiness_receipt_is_engine_not_ready_not_a_storage_outage[postgresql]
  ```

**反例 2 —— 去掉主动续期**：`renewed = readiness.expires_at_us <= now_us or renew_readiness`
→ `renewed = readiness.expires_at_us <= now_us`。

```text
assert renewed.readiness_renewed is True
E       AssertionError: assert False is True
FAILED tests/unit/backend/test_engine_registration_transaction.py::test_existing_result_renews_a_still_valid_receipt_only_when_asked
FAILED tests/integration/test_engine_registration_pg.py::test_the_report_and_the_renewal_expose_an_expiring_then_expired_receipt[postgresql]
2 failed, 154 passed in 6.97s
```

**反例 3 —— 把存储方向改写成新码**：`job_error()` 里 `job_unavailable` → `engine_not_ready`。

```text
assert body["error"]["code"] == "job_unavailable"
E       AssertionError: assert 'engine_not_ready' == 'job_unavailable'
FAILED tests/contract/test_public_job_engine_readiness.py::test_a_store_that_really_refused_the_read_keeps_the_outage_answer

# 真实存储故障（撤销 admission 角色对 workspace_quotas 的 SELECT）：
assert ('engine_not_..._error', True) == ('job_unavail...error', False)
FAILED tests/integration/test_public_job_admission_http_pg.py::test_a_failed_quota_read_keeps_the_storage_answer_while_readiness_is_healthy[postgresql]
```

**反例 4 —— `expiry_state()` 恒返回 `valid`**：

```text
FAILED tests/unit/backend/test_engine_readiness_report.py::test_the_report_separates_valid_expiring_and_expired_against_one_clock
FAILED tests/unit/backend/test_engine_readiness_report.py::test_main_reports_every_instance_then_a_summary_and_exits_zero_for_valid
FAILED tests/unit/backend/test_engine_readiness_report.py::test_main_fails_on_an_expired_receipt_so_a_timer_or_probe_can_alert
FAILED tests/unit/backend/test_engine_readiness_report.py::test_main_can_treat_an_expiring_receipt_as_an_alert
FAILED tests/unit/backend/test_runtime_readiness_expiry.py::test_the_expiry_state_reports_the_remaining_window[1040000001-60000000-expiring]
FAILED tests/unit/backend/test_runtime_readiness_expiry.py::test_the_expiry_state_reports_the_remaining_window[1060000000-60000000-expired]
6 failed, 12 passed in 0.15s
```

**断言强度评价：够。** 四条都是「改一行生产代码就变红」，且每条都能定位到**行为**用例而不是
纯文本守卫；两条直接打在真实 PG + 生产 HTTP 装配上。唯一较弱的是 `tests/ci` 那 193 行：
它是纯文本 regex 守卫（改注释、改文档就会红），只证明「装配没被删掉」，作者也这么声明了，
不构成语义证据——这点在文档里写清了，可接受。

### C10 两方向区分是否真的成立

**成立。** 我在一次性 PG 16.15 + 生产 HTTP 装配（`create_app(settings, jobs=PublicJobs(store, store))`）
上独立复现（不是引作者的贴图）：

过期收据（`expires_at_us = now - 1µs`，实例仍 `ready`）：

```text
503 {"error": {"code": "engine_not_ready", "message": "The engine readiness receipt for the
     pinned instance has expired; register the engine again, then replay the same request.",
     "param": null, "request_id": "c42a950055234d3cb7c033c099de9845", "retryable": true,
     "type": "service_unavailable_error"}}
state unchanged: True
```

重签收据后**同一 Idempotency-Key**：

```text
202 {"artifacts": [], "cleanup_state": "not_required", "client_reference": "synthetic-reference",
     "created_at": "2026-11-15T08:39:09.762809Z", "effective_configuration": {...},
     ...}
```

真实存储故障（`REVOKE SELECT ON workspace_quotas FROM <admission role>`，收据全程有效）：

```text
503 {"error": {"code": "job_unavailable", "message": "Job storage is unavailable.",
     "param": null, "request_id": "74c4128f60704c9296cb5e0d9e9ea5d1", "retryable": false,
     "type": "service_unavailable_error"}}
state unchanged: True
```

两个方向的 `code`、`message`、`retryable` 全部不同，且都不写行（`_snapshot()` 前后一致由用例
断言并通过）。**注意这正是 A1/A4 的问题所在：分得开的是「过期」，不是「同类」。**

## D. 范围与例数

### D11 diff 范围与生成产物

范围与预期一致（32 文件）：admission 折叠（`public_job_admission.py`、`domain/admission.py`、
`runtime_observations.py`）、错误映射（`job_errors.py`）、契约与生成物
（`speech-openapi.yaml`、`packages/contracts/{source-inventory.json,speech.generated.ts}`）、
续期/报告命令（`engine_registration.py`、`engine_readiness.py`、`backend/pyproject.toml`）、
部署装配（`deploy/{compose.yaml,compose.env.example,job-startup-gate.py,README.md}`）、
消费者（`web/workbench/...`、`voice_drafts.py`）、测试、文档（`async-job-runbook.md`、
`register-engine.md`、`docs/validation/2026-11-15-engine-readiness-expiry.md`）。没有夹带
无关改动。

**生成产物未被手改——两项独立证据：**

1. `packages/contracts/source-inventory.json` 记录的 `speech-openapi.yaml` sha256
   `6f48e2bc…878af8` 与 `shasum -a 256 contracts/speech-openapi.yaml` 实测**完全一致**，
   说明清单是被生成器重算的而不是手写。
2. 等价 `make contracts-drift`（`uv run python scripts/dev-env/generate-contract-bindings.py --check`）：

   ```text
   Found 36 errors (36 fixed, 0 remaining).
   Contract bindings: up to date
   exit 0
   ```

   即 `speech.generated.ts` 与生成器输出逐字节一致。（该步骤需要 `pnpm install`，我在
   `sn843-run` 里装过再跑；审查分支未受影响。）

### D12 例数与 `gh pr checks 839`

| 套件 | PR head 实测 | `origin/main`（`c672b6b2`）实测 | 差值 | 作者自报 | 一致 |
| --- | --- | --- | --- | --- | --- |
| `tests/unit/backend` | **3294** | **3273** | +21 | 3294 / 基线 3273 | ✓ |
| `tests/unit` | 7154（含 3 skip → **7151 passed**） | 7133 | +21 | 7151 passed + 3 skipped | ✓ |
| `tests/contract` | **3825** | **3813** | +12 | 3825 / 基线 3813 | ✓ |
| `tests/ci` | **408** | **399** | +9 | 408 / 基线 398 | ✓（基线见 D13） |
| 集成 `test_engine_registration_pg.py`（5）+ `test_public_job_admission_http_pg.py`（8） | **13 passed** | — | — | 13 passed | ✓ |
| `test_production_admission.py -k authority_windows` | **8 passed** | — | — | 8 passed | ✓ |

全量跑：`uv run pytest tests/unit tests/contract tests/ci -q` →
`11384 passed, 3 skipped, 1357 subtests passed in 287.01s`（7151+3825+408=11384 ✓）。

`gh pr checks 839`（2026-11-15 16:34）：26 项 **pass**、0 fail、1 项 skipping
（`Docs: markdown code block formatting`）。Python tests、Python lint/types/contract
compatibility、Web、六个 Integration shard、Compose 校验、六项兄弟门禁全绿。

### D13 作者未定位的基线差值

作者写「rebase 前本机 `unit/backend=3245`、`ci=370`，与任务给出的 `3272`/`390` 有差；rebase 到
`5cd5a5d0` 后为 `3273`/`398`（#833 多 28 个用例），差值来源仍未定位」。

我在**干净 `origin/main` 检出**上实测：`tests/unit/backend = 3273`、`tests/ci = 399`。
定位结果：

- `3272 → 3273`：**不是差值**。干净 `origin/main` 与小写基线都是 3273，任务给的 `3272` 只是
  一个偏一的口径（`5cd5a5d0..origin/main` 对 `tests/unit/**` 无任何改动，只有
  `tests/ci/test_web_image_context.py` 变了）。与本 PR 无关。
- `390 → 398 → 399` 的全部来源：`merge-base = 5cd5a5d0`；它到 `origin/main` 之间只有两个提交，
  `#838`（`c672b6b2`，纯文档）与 `#841`（`fb8f8fa8`），后者在
  `tests/ci/test_web_image_context.py` 新增 **1** 个用例
  （`test_a_policy_only_root_input_is_reported_as_policy_not_as_a_build_failure`）。
  所以：`390`（任务给的旧口径）→ `398`（`5cd5a5d0`，含 #833 的 workbench-api 用例）→ `399`
  （当前 `origin/main`，含 #841 的 +1）。**作者的 398 对他自己的 merge-base 是对的，
  只是 `origin/main` 在此之后又 +1；与本 PR 无关。**

结论：作者那句「差值来源未定位」可以撤销——是任务口径过旧 + merge-base 之后的 #841，
两项都与本 PR 无关。这一点我在报告里给出实测值，不需要作者再猜。

## 必须修清单

**B1（阻塞）`retryable` 取值与契约内先例冲突，且无 `Retry-After`。**
`engine_not_ready` 与 `workspace_not_configured` 是同一形状（都要一次带外运维动作，同一 key
随后可放行），前者 `retryable: true`、后者 `retryable: false`。按契约 `retryable` 的定义
（"after the refusal"），立即重试不会成功，`description` 自己就写了
"retrying before the receipt is re-signed cannot succeed"——**字段与描述矛盾**。二选一：
(a) 改 `retryable: false`，与 `workspace_not_configured` 对齐（推荐，语义最稳）；
(b) 保留 `true` 但**必须**补 `Retry-After`（例如等于续期间隔或 TTL，给出「什么时候重试才有
意义」的可执行提示），并在 description/`errors.schema.json` 的 `retryable` 说明里补一句
「带外恢复」这一类。同时把 `tests/contract/test_public_job_engine_readiness.py:90-96`
与新声明对齐。

**B2（阻塞）契约只在 `description` 声明，消费者无法机器发现。**
给 `createJob` 的 503 加逐响应码枚举（`allOf: [$ref ErrorResponse] + code.enum`，
四个码：`job_unavailable` / `job_outcome_unknown` / `job_admission_contended` /
`engine_not_ready`；用 `enum` 不用 `const`，该状态被四个码共用），跑
`make contracts-generate` + `make contracts-drift`。共享 `errors.schema.json` 的 `code`
**不要**改成枚举（跨四个协议，逐操作词表不该进共享定义）。同类收窄可顺带覆盖
`workspace_not_configured` 所在的 422，避免下一个码再只写描述。

**B3（阻塞）同类余量必须交代清楚，不能只留注释。**
本 PR 之后 `reconciliation_required`、`policy_or_resource_disabled`、`version_conflict`、
`stale_fencing_token`、`recipe_mismatch`、`lease_not_active` 仍回答
`503 job_unavailable` + "Job storage is unavailable."（我已实测 `reconciliation_required`
仍复现该响应原文）。要么在本 PR 内把**至少 `policy_or_resource_disabled`（实例非 ready）** 一并
拆出（它与续期同属部署侧可恢复），要么：
- 在 `contracts/speech-openapi.yaml` 的 `createJob` 描述里明确写出「本码不覆盖哪些情形」，
  让调用方知道 `job_unavailable` 不必然意味着存储；
- 开一张跟踪 issue，把这些类的收窄列为后续契约变更（并在
  `_PUBLIC_CODES` 的注释里指向它，而不是只写「a new entry is a contract change」）。

**B4（阻塞，小改动）消费者注释已过期。**
`web/workbench/src/file-workflow/file-task-session.ts:59-62` 的
「exactly 三个 retryable 码」现在是四个；引用的 `job_errors.py:129-134` / `:113` 行号失效
（实际 158-163 / 127）。这是本 PR 修改的同一段注释块，顺手改掉，否则它继续为下一位读者提供
错误依据。

## 建议修（非阻塞）

- **S1** `docs/operations/register-engine.md` 的「`remaining_seconds`（已过期为负）」与边界行为
  不符：`now_us == expires_at_us` 时状态是 `expired` 但剩余秒数是 `0`。要么文案改成「非正」，
  要么实现改成 `remaining_us / 1e6` 的浮点/向上取整。
- **S2** `retryable` 依据里引用的 `job_admission_contended` 在当前创建路径**不可达**：
  `_RetryAdmissionError` 只在 `attempt == 0` 抛（`public_jobs_postgres.py:592`），而处理分支
  `if attempt == 0: continue` 之后 `attempt` 必为 1，`version_conflict` 在 attempt 1 被直接折叠
  成 `job_unavailable`（`:593`）。即 `job_admission_contended` 的 `return` 是死分支，
  连带 `_ADMISSION_REJECTION_CODES` 与工作台表里的该行都指向一个生产不可达的码（既有问题，
  非本 PR 引入，但本 PR 把它当作 `retryable` 先例）。
- **S3** `engine_registration` 的 `expiring` 预警窗口用**本次调用的** `--readiness-ttl-seconds // 4`
  而不是收据自带的窗口；若运维只给续期命令传了更短/更长的 TTL，预警窗口与真实窗口不一致。
  用 `expires_at_us - verified_at_us` 的四分之一更稳。
- **S4** `deploy/compose.yaml` 的两个新服务的 `tmpfs` 写法不一致：
  `engine-readiness*` 用 `- /tmp:size=16m,mode=1777`，报告服务用 `- /tmp:size:8m,mode:1777`
  （冒号 vs 等号）。Compose 两种都接受，但同一文件里两种写法容易在评审时被当成笔误。
- **S5** `/readyz` 的缺口建议单开一张卡：把「创建可准入」做成独立可轮询面（`checks[]` 里加
  `job_admission: degraded` 而不改 200，或 `GET /v1/nexus/jobs/admission-readiness`），
  让它不再依赖运维自建 cron（#840 类风险）。

## 只能依赖作者陈述的点

| 事项 | 原因 |
| --- | --- |
| 真实引擎的 `/health` 探测 | 本机没有真实引擎，按项目约定不用 fake 代替。`--renew-readiness` 走的是与注册相同的探测与事务路径，我用本地 HTTP 桩验证了事务与 `verified_at_us` 递增，**未**验证真实模型端点 |
| `docker compose --profile engine-readiness run --rm …` 的实际容器启动 | 我只核了 Compose 可解析、服务/参数/环境接线与 `tests/ci` 文本守卫；未构建镜像跑容器 |
| 宿主机 cron/systemd 定时器本身 | 未安装任何定时任务；文档声称的运维步骤按陈述接受 |
| `docs/validation/2026-11-15-engine-readiness-expiry.md` 里「rebase 前 3245/370」 | 那是作者的中间时点；我只核了 `5cd5a5d0` 与 `origin/main` 两个可复现点 |
| `scripts/dev-env/check-doc-refs.py` 的 `RESULT FAIL`（AMBIGUOUS=64/MISSING=27）为既有状态 | 我未重跑该项；作者说明在暂存全部改动后失败项完全一致，按陈述接受 |

## 复现命令

```bash
# 审查 worktree（报告唯一差异）
git worktree add /private/tmp/sn843 -b review/843-readiness-error-code origin/main
# 被审代码（只读上下文 + 变异副本）
git worktree add --detach /private/tmp/sn843-run 956b43d426a840b750f405a043a325950089df15
cd /private/tmp/sn843-run && uv sync --frozen --all-packages && pnpm install --frozen-lockfile

# 例数
for d in tests/unit/backend tests/unit tests/contract tests/ci; do
  uv run python -m pytest "$d" --collect-only -q -p no:cacheprovider | tail -1
done

# 全量
uv run pytest tests/unit tests/contract tests/ci -q -p no:cacheprovider

# 生成产物
uv run python scripts/dev-env/generate-contract-bindings.py --check

# 一次性 PG/MySQL（用完 docker rm -f）
docker run -d --name sn843-pg -e POSTGRES_USER=speechnexus -e POSTGRES_PASSWORD=speechnexus_test \
  -e POSTGRES_DB=speechnexus_test -p 127.0.0.1:55443:5432 postgres:16.15-alpine
docker run -d --name sn843-mysql -e MYSQL_ROOT_PASSWORD=speechnexus_test \
  -p 127.0.0.1:55444:3306 mysql:8.4.8
SPEECHNEXUS_TEST_DATABASE_URL='postgresql://speechnexus:speechnexus_test@127.0.0.1:55443/speechnexus_test' \
SPEECHNEXUS_TEST_MYSQL_URL='mysql://root:speechnexus_test@127.0.0.1:55444/' \
  uv run pytest tests/integration/test_engine_registration_pg.py \
                 tests/integration/test_public_job_admission_http_pg.py -q
```

## 清理

`/private/tmp/sn843-run` worktree、`sn843-pg` 与 `sn843-mysql` 容器、探针脚本与临时日志
（`/tmp/ro*.log`、`/tmp/drift*.log`、`/tmp/sn843tmp*`）在审查结束时全部移除；
未触碰 `sn838-pg`、`speechnexus-coordination-mysql`、`sn832-workbench-api-1` 等既有实例，
未停止或修改任何共享 GPU 上的引擎，未 push main，未合并。
