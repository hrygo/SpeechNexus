---
title: "HTTP + 生产准入跨层测试（PR #700 / issue #698）的非作者审查"
status: active
version: "0.1.0"
date: 2026-11-14
---

# HTTP + 生产准入跨层测试的非作者审查（PR #700 / #698）

- **审查对象**：`002c9a4b03030dba485188e790340b006d7d9db7`（`test: 补 HTTP + 生产准入装配的跨层集成覆盖 (#698)`），PR #700（分支 `test/699-admission-http`，base `main`），对应 issue #698。改动面 1 文件、+323/−0，只新增 `tests/integration/test_public_job_admission_http_pg.py`。
- **基线**：审查开始时 `origin/main` = `be8ac046`（#692），被审 head `002c9a4b` 尚未合入 main。`git diff --name-only be8ac046...002c9a4b` 只有该测试文件（`git show --stat 002c9a4b` 同结论）；交付分支基于 `origin/main` 只新增本报告，不把未合入的 #700 带进审查 PR。
- **工作区**：独立 worktree `/private/tmp/sn704`，先 `git worktree add /private/tmp/sn704 -b review/704-admission-http 002c9a4b` 读取被审内容；交付时分支改基于 `origin/main`（`be8ac046`）只提交本报告。主检出停在 `a7508635 docs/tracker-batch12` 且 `uv.lock` 有未提交改动，本报告未读取其工作树；产品事实取自 worktree 与 `git show <rev>:<path>`。
- **审查者**：非作者。交付分支只新增本报告，产品代码与测试零改动。变异实验在临时 worktree `/private/tmp/sn704-mut`（同 commit、分支 `mut/704-mutation`）进行，每次实验后 `git checkout -- .` 还原；该 worktree、分支与探针文件已删除，未 push 任何变异。
- **隔离环境**：本机 PostgreSQL 上的一次性专用库 `sn_r704_admission`（审查结束 `dropdb`）+ 既有项目隔离 MySQL 容器 `speechnexus-coordination-mysql`（`127.0.0.1:55434`，只连接、未启停）。DSN 只经环境变量传入，未连共享或生产实例；本文件不需要 MinIO/浏览器前置。
- **审查方式**：静态审查（装配链、契约、错误映射、迁移种子）+ 本机实跑目标文件 + 4 组产品侧变异 + 1 组判别探针 + CI shard artifact 复核 + 边界与 `uv.lock` 核查。
- **报告文件名**：沿用任务指定的 `2026-11-13`；实际执行在 2026-11-14 凌晨（+08:00），时间线按会话锚点相对记录。

> **事实分层**：标「实跑」的都有本机命令输出；只做代码/文档推导的，在「只能依赖作者陈述或未实测」一节单列。

---

## 结论

**有条件通过。**

这份测试的骨架是真的：装配确实是生产同款 `PostgresPublicJobs(require_admission=True)` 加 `create_app(jobs=...)`，准入是真的（去掉准入池，成功用例立刻 503 红），10 张表的行差断言也有牙（在拒绝路径上写一行，用例 4 当场红）。我核对到 CI 的六分片 artifact 里本文件 5 个用例确实 `executed=true`、全部 `passed`，且 artifact 记录的 merge commit 中该文件 blob 与被审 head 完全相同，不存在"靠跳过通过"。

但 PR 最有价值的那条证据——"请求体超过接收上限被拒"——用例 4 并不真的能证伪：**把 `max_bytes` 放宽到 `1048576`（等于禁用接收上限）后，用例 4 仍然绿**，因为它的 100000 个 CJK 字符同时撞上 4096 的计划上限，400 由后者给出。也就是说，本文件对 #703（未声明的请求体上限）没有回归保护；docstring 的 "What this case pins is the order: the receive bound refuses before any reservation" 属于过度声称。

修掉 M1 后可合并；其余为建议，不阻塞。

---

## 时间线

| 时点 | 动作 | 结果 |
| --- | --- | --- |
| T0 | `git fetch`、建 worktree、`git show --stat 002c9a4b` | head `002c9a4b`；1 文件 +323/−0；`origin/main` = `be8ac046` |
| T1 | 读 `tests/integration/AGENTS.md`（DSN 失败语义、独立 schema、双 DSN）与目标文件 323 行 | 5 个用例；复用 `_counts`/`_seed`/`_store`/`_settings` |
| T2 | 读产品装配与准入链 | `job_runtime.py:252-258` 生产同款；`public_jobs_postgres.py:191/513-527`；`public_job_admission.py:102-107/159-166/587/678`；`job_errors.py:9/113` |
| T3 | 读契约与迁移种子 | `TtsJobInput.text.maxLength: 200000`（4164-4173）；createJob 响应无 413；`PayloadTooLarge` 组件被别处使用；`asset_schema.py:15` 种子 `workspace-default` 且无配额行 |
| T4 | 起隔离环境、`uv sync --frozen`、实跑目标文件（Python 3.14.9 / pytest 9.1.1） | `5 passed in 1.73s` |
| T5 | 复核 CI：run `43502946874` 与 6 个 shard artifact | headSha = `002c9a4b`；shard 1/2/3 `executed=true`；5 个用例 `passed`；artifact `source_head` = `b9d822a`（PR merge commit），测试文件 blob 与 head 同为 `3fb1e00f…` |
| T6 | 变异 A（拒绝路径写一行）、变异 B（去掉准入池） | 用例 4 红（行差 1 vs 0）；用例 1 红（503） |
| T7 | 变异 C（默认上限 262144→524288）、变异 D（装配点上限改 1048576） | C 红于 `300175 > 524288`；**D 仍绿** |
| T8 | 判别探针：`text` 4096 码点 + 300000 字节 padding | 有接收上限 → 400 `invalid_job_request`；无 → 422 `unsupported_parameter` |
| T9 | 契约不符探针：无名 workspace、4097/100000/200000/200001 字符、坏 JSON | 见「四个契约不符」表 |
| T10 | 边界核查 | 仅 1 文件；`uv.lock` 未改；`git status --porcelain` 空；`ruff format --check` 全绿；`ruff check` = `All checks passed!` |
| T11 | 清理：删变异 worktree/分支、`dropdb sn_r704_admission`、删临时 ref | worktree 列表只剩既有项 |
| T12 | 写本报告、复跑格式化与 lint、commit/push/开 PR | 见文末校验记录 |

---

## 七条逐条判定

### 1. 装配是否真的是「生产准入」——成立

- 生产组合根 `backend/src/speechnexus_backend/job_runtime.py:252-258`：

  ```python
  adapter = PostgresPublicJobs(
      pool,
      admission_pool=admission_pool,
      require_admission=True,
      timeout_seconds=self._settings.job_transaction_timeout_seconds,
      storage_bucket=self._settings.object_storage_bucket,
  )
  self.jobs = PublicJobs(adapter, adapter)
  ```

  测试的 `_app()`（62-72 行）用共享 helper `_store()`（`test_public_job_atomic_admission_pg.py:184-192`）构造同一组参数（`require_admission=True` + 真 `admission_pool`），再 `create_app(settings=..., jobs=PublicJobs(store, store))`；`create_app` 对注入的 `jobs` 是直接透传（`app.py:511-514`），没有包装或替换。
- 准入真的生效，而不是"挂着 `require_admission=True` 但走替身"：**实跑**变异 B 把 `_admission_pool` 强制置空后，成功用例立刻 `assert 503 == 202`，body 为 `job_unavailable`；同时在正常运行时成功用例断言了 `admission_attempts`、`admission_requests`、`admission_leases`、`coordination_jobs`、`coordination_outbox` 各 +1（10 张表全部 +1），这是只有真准入事务才会有的行。
- 限制（不影响放行）：它是"手搭同参数"，不是调用 `JobRuntime`/`create_app_from_env` 本身；生产若给该构造加新参数（例如新增必填 kwarg），本文件不会因装配漂移而红。参数差异：测试用 `timeout_seconds=3`、`storage_bucket="transcription-inputs"`，生产取 settings。建议在 `_app` 旁注明"参数须与 `job_runtime.py` 保持同步"。

### 2. 10 张表 delta 是否真的覆盖"拒绝无副作用"——成立

- `_counts()`（`test_public_job_atomic_admission_pg.py:253-268`）覆盖 10 张表：`speech_jobs`、`speech_job_events`、`speech_job_submissions`、`speech_job_intents`、`admission_operations`、`admission_attempts`、`admission_requests`、`admission_leases`、`coordination_jobs`、`coordination_outbox`。
- **实跑变异 A**（唯一一条按清单独立复现的反例）：在 `_create()` 的 `_TooLargeError` 分支里，先对同一个 store 做一次合法 `submit()` 再返回原来的 400。用例 4 随即红，失败点正是零副作用断言：

  ```text
  >           assert _counts(db) == before
  E           AssertionError: assert {'speech_jobs...} == {'speech_jobs...}
  E             Differing items: {'speech_job_intents': 1} != {'speech_job_intents': 0}
  E             ... {'speech_jobs': 1} != {'speech_jobs': 0} ...
  ```

  状态码与错误码断言仍是 400 `invalid_job_request`，说明这条断言的灵敏度独立于状态码。
- 覆盖边界：`_counts` 不含 `workspace_quotas` 与 `speech_workspaces`。拒绝路径若只做一次配额 `UPDATE` 并提交、不插任何行，本文件发现不了；实际准入把占用与行写在同一个事务里（`public_job_admission.py` 的 identity/reservation 路径），所以现实中可接受。重放用例另行断言了 `held_attempts`（见第 3 条）。

### 3. 五条用例的断言强度——三条要求全部满足

- **成功路径三处一致**：`job["workspace_id"] == "workspace-test"`（129 行）、`speech_jobs.workspace_id` 查询一致（141-143 行）、intent 的 `request_document.workspace_id` 一致（133-140 行），并且 `SELECT job_id FROM speech_job_intents` 只有一行（144 行），排除了"响应与持久化各写一份"的假一致。
- **超限用例排除"其实是请求体超限"**：`assert _wire_bytes(over_limit) < _settings().job_receive_max_bytes`（177 行），实跑值 12,466 B，远低于 262,144 B，这个混淆被明确排除。
- **重放断言 `held_attempts`**：`SELECT held_attempts FROM workspace_quotas` == `1`（316 行），即创建后占用 1、重放后仍是 1；配合"行数不变 + 同 id + 同 Location"，能识别"重放又占了一次"。
- 小瑕疵（建议）：这条 SQL 没有 `WHERE workspace_id = ...`，靠"库内只有一行配额"成立；写法上不如断言 before/after 两个快照。若将来同一 schema 里加第二个 workspace 配额，这条会误报。

### 4. 有没有假绿——不存在跳过式通过，CI 证据真实

- **无跳过路径**：模块内没有 `pytest.skip`/`xfail`；`admission_database`/`database` fixture 在缺 DSN 时是 `pytest.fail` 而非 skip（`test_production_jobs.py:110-114`）。**实跑** `test_pg_dsn_skip_guard.py` → `15 passed`，该文件未进白名单。
- **CI artifact 复核**（不是采信作者转述）：run `43502946874` 的 `headSha` = `002c9a4b`（PR head）。下载 6 个 `integration-shard-*` artifact 后逐个解析：

  | 分片 | `executed` | `exit_code` | 本文件用例 | `outcomes` |
  | --- | --- | --- | --- | --- |
  | shard-1 | `true` | 0 | `test_body_over_the_receive_bound_is_refused_before_any_write` | `passed` |
  | shard-2 | `true` | 0 | 成功 / 4096-4097 / 重放 三条 | `passed` |
  | shard-3 | `true` | 0 | `test_create_without_a_provisioned_workspace_quota_is_refused_without_rows` | `passed` |

  5 个用例全部出现在 `selected` 且 `outcomes` 为 `passed`，没有跳过、没有 collect-only。artifact 里的 `source_head` 是 `b9d822a`（PR 的 ephemeral merge commit，`pull_request` 事件的 `GITHUB_SHA`），不是分支 head；我把 `refs/pull/700/merge` 取回比较，该文件在两个 commit 的 blob 同为 `3fb1e00f`，所以证据确实覆盖被审内容。
- **本机复跑**（见下节）：`5 passed in 1.73s`，与 CI 独立。

### 5. 四个「产品行为与契约不符」——全部成立，但其中两条的 issue 号与任务描述错位

见下一节逐条证据。要点：任务里「请求体上限 → #702」与「默认 workspace 无配额 → #703」对调了；仓库实际是 **#702 = 503 配额**、**#703 = 未声明请求体上限**。本报告按仓库事实编号，PR 描述与 docstring 只引用了 #690，无编号错误。

### 6. 新绿/新红——存在一处"前提失效即红"的耦合，以及一处必须修的过度声称

- **实跑变异 C**：把 `settings.py:174` 默认 `job_receive_max_bytes` 从 262144 调到 524288，用例 4 红在 `assert 300175 > 524288`。这不是误报：默认值调大后该场景（契约合法但字节超限）确实消失，测试应当重新设计；但断言消息没有说明"这是前提守卫"。docstring 只写了 "the deployment's 262144-byte receive bound"（事实陈述），没有写"默认值上调时本用例必须重做"。建议加一行注释区分"行为断言"与"场景前提守卫"。
- **更强的耦合**：`PLAN_TEXT_CEILING = 4096`、`CONTRACT_TEXT_CEILING = 200_000` 与 #690 绑定。按 #690 的验收标准（`TtsJobInput.text.maxLength` 改为 4096）落地后，用例 2 的 4096/4097 边界、用例 4 的"正文仍在契约 200000 内"前提都会失效，必须同步改；同理 #702 修好后用例 3 的 503 断言会红。docstring 已写明 "Issue #690 owns the contract correction"，属已知耦合，但建议在 PR 描述里显式列出"修 #690/#702 时同步更新本文件"，避免修复者误以为是自己写坏了。
- **不算问题的新红**：`PLAN_TEXT_CEILING`/`CONTRACT_TEXT_CEILING` 都是带出处的常量而非魔法数字，改产品常量时会红，这是想要的 pin。

### 7. 边界——干净

- `git show --stat 002c9a4b` 与 `git diff --name-only be8ac046...002c9a4b` 都只有 `tests/integration/test_public_job_admission_http_pg.py`；`backend/src/`、`contracts/`、`web/`、`.github/workflows/`、`scripts/dev-env/` 零改动，没有放宽任何既有校验。
- `uv.lock` 未改动（worktree `git status --porcelain` 为空；`uv sync --frozen` 未回写）。交付分支提交前再次核对。
- 可维护性建议：复用其他测试模块的私有 helper（`_seed`/`_counts`/`_store`、unit 的 `_settings`）让本文件与三个模块耦合；如果 `_counts` 的表清单变化，本文件的"10 张表"预期会跟着变，方向通常是安全的（少表会静默变弱）。建议至少断言 `len(before) == 10`，或在 docstring 固定当前清单。

---

## 四个「契约不符」是否成立

| # | 主张 | 判定 | 归属 issue（仓库实际） |
| --- | --- | --- | --- |
| 1 | TTS 正文契约 200000，产品准入只允许 4096 | **成立** | #690 |
| 2 | 请求体上限未在契约声明，超限返回 400 而非 413 `PayloadTooLarge` | **成立** | #703（非任务描述里的 #702） |
| 3 | 默认 workspace 无配额行 → 503 `Job storage is unavailable.` | **成立** | #702（非任务描述里的 #703） |
| 4 | 错误码不可区分多种原因 | **基本成立**（非"五因一码"，见下） | #702/#703 与 #690 的公共症状 |

### 1) 200000 vs 4096 —— 成立（#690）

- 契约 `contracts/speech-openapi.yaml:4164-4173`：`TtsJobInput.text` `maxLength: 200000`；同一契约的同步路由 `/v1/audio/speech` 写的是 `4096`。
- 产品：`speech/domain/speech_synthesis.py:21` `MAX_SPEECH_TEXT_CHARS = 4096`，`SpeechPlan.__post_init__`（197 行）校验 `1 <= len(text) <= 4096`；准入在 `public_job_admission.py:678` 用全量正文重建 `SpeechPlan`。
- **实跑**（隔离 PG + 真 HTTP + 生产准入装配）：4096 CJK → 202；4097 CJK → 400 `invalid_job_request`；200000 ASCII（wire 200,175 B < 262,144 B，排除了字节上限）→ 400 `invalid_job_request`。
- 附带修正 #690 自身的一个论断：它写"4096 字符最大约 12KB，字节上界在任何合法 Job 上不可达"。这在契约改成 4096 之后成立；**今天**契约允许 200000 字符，100000 个 CJK 字符（字符数 100,000 ≤ 契约 200,000，UTF-8 约 300,175 B）就能撞穿 262,144 字节上限，所以本 PR 用例 4 的场景是真实的契约内请求。

### 2) 未声明的请求体上限 + 400 而非 413 —— 成立（#703）

- 契约 `createJob`（798 行起）的响应只有 202/400/401/403/404/405/409/422/429/503，**没有 413**；请求体段也没有任何字节上限声明（全文搜不到 262144 一类上界）。`components.responses.PayloadTooLarge` 存在且被 `/v1/audio/transcriptions`（179-180 行）与资产上传（1252-1253 行）使用，说明契约本身有 413 语义，只是 Job 创建没接。
- 产品：唯一上限装配点是 `app.py:749`（`max_bytes=resolved_settings.job_receive_max_bytes`，默认 262144，`settings.py:174`）；超限在 `jobs.py:391/402` 抛 `_TooLargeError`，`jobs.py:452-453` 映射成 400 `invalid_job_request`，没有 413、没有 `PayloadTooLarge`。
- **实跑**：wire 300,175 B 的请求 → 400 `invalid_job_request`，0 行写入。
- 复核结论：PR docstring 的说法准确，但**用例 4 无法证明这一条**（见 M1）。

### 3) 默认 workspace 无配额行 → 503 —— 成立（#702）

- 种子来源：`speech/infrastructure/asset_schema.py:14-16` 在建表 DDL 里插入 `workspace-default`；**没有任何迁移插入 `workspace_quotas`**（`migrations/` 与 `coordination_schema.py` 只建表）。配额行唯一创建点是引擎注册 `engine_registration.py:470-475` 的 `_ensure_quota`（操作者动作），以及管理面 workbench 的工作区写入口（同样不带配额）。
- 拒绝机制：`public_job_admission.py:159-166` 的 `_quota()` 走 `_one()`（102-107 行），0 行即抛 `PublicJobAdmissionRejectedError("job_unavailable")`；`job_errors.py:113` 把 `job_unavailable` 映射为 503 / `service_unavailable_error` / `retryable=false` / 消息 "Job storage is unavailable."。
- **实跑**：省略 `workspace_id` 的 body 先过契约（`parse("JobCreateRequest", body)` 成功，键为 `client_reference, input, kind, model, voice`；`workspace_id` 是可选项），服务器代入 `workspace-default`，随后得到
  `503 {"code":"job_unavailable","type":"service_unavailable_error","retryable":false,"message":"Job storage is unavailable."}`，0 行写入。
- 该断言确实复现了"schema 合法请求得 503"，并且用例第 3 条还在补一行配额后让同一个无名请求变 202，证明拒绝原因是缺配额行而不是请求文档不合法——这是该用例设计上最强的一点。

### 4) 错误码不可区分 —— 基本成立，但不是"五种原因一个码"

- 契约只约束形状：`contracts/shared/errors.schema.json:31-35`，`error.code` 只有 `pattern: "^[a-z][a-z0-9_]{2,63}$"`，没有任何 Job 创建错误码枚举；`createJob` 的 400/422 都只是通用 `ErrorResponse`。所以客户端无法从契约枚举这些原因。
- **实跑**对照（同一装配、同一 seed）：

  | 场景 | wire | status | code | message |
  | --- | --- | --- | --- | --- |
  | 4097 CJK（计划上限） | 12,466 B | 400 | `invalid_job_request` | "Invalid Job request." |
  | 100000 CJK（接收上限） | 300,175 B | 400 | `invalid_job_request` | "Invalid Job request." |
  | 200000 ASCII（计划上限） | 200,175 B | 400 | `invalid_job_request` | "Invalid Job request." |
  | 200001 ASCII（仅违反契约 maxLength） | 200,176 B | 422 | `unsupported_parameter` | "The Job request is not supported." |
  | 截断 JSON | — | 400 | `invalid_job_request` | "Invalid Job request." |

- 结论：计划上限、接收上限、坏 JSON 三类**共用** 400 `invalid_job_request`，4097–200000 之间的拒绝无法归因；契约 `maxLength` 越界走的是 422 `unsupported_parameter`（可区分，但消息仍不说明"太长"）。所以 PR 的"不可区分"结论成立，措辞应从"五种原因一个码"收敛为"三类共用一个 400 码，且契约上限越界只得到 422"。

---

## 必须修清单

### M1. 用例 4 必须能证伪"接收上限"（否则 #703 无回归保护）

- **证据**：变异 D 把唯一装配点 `app.py:749` 的 `max_bytes` 改成 `1048576`（等于禁用接收上限）后，用例 4 仍然 `1 passed`。原因：`text = "中" * 100_000` 同时超过计划上限 4096，禁用接收上限后 400 依旧由 `SpeechPlan` 给出，`_counts` 也依旧不变。也就是说本文件对"删除/放宽接收上限"这一回归不设防。
- **同时要修 docstring**：262-263 行 "What this case pins is the order: the receive bound refuses before any reservation or public row is written" 与实测不符——它 pin 的是"超限 body 的拒绝发生在写入前"，不区分是哪条上限。
- **建议改法**（已在临时 worktree 验证可行）：用"正文在计划上限内、只有字节数超界"的 body，让两条上限在结果上可分。
  - 现状下 `text` 是唯一能撑大 body 的字段（`client_reference` 256、其余字段都很小），所以用 schema 非法但**在接收阶段就被拒**的 padding 来区分顺序：`text = "中" * 4096` 加 `body["padding"] = "a" * (max_bytes + 1024)`（padding 长度按 `_settings().job_receive_max_bytes` 计算，可自适应当前默认值）。
  - 判别探针实跑：接收上限存在 → `400 invalid_job_request`、0 行；把上限放宽到 1048576 → `422 unsupported_parameter`、0 行。前者对应"接收上限先于 JSON 解析拒绝"，后者对应"没有接收上限时进入 parse"。
  - 修好后请用"禁用接收上限 → 用例必须红"自查一次，并把 docstring 改成"本用例断言接收上限在解析与写入之前拒绝"，不要再声称它区分计划上限。
  - 退路（不推荐）：若短期不打算把接收上限做成可测行为，就把 docstring 与 PR 描述降级为"超限 body 的拒绝发生在写入前"，并显式写明本文件不覆盖接收上限；但这样 #703 仍然没有回归保护，需要另开测试任务承接。

---

## 独立复现的反例（变异实验）

| 变异 | 改动 | 期望 | 实际 | 结论 |
| --- | --- | --- | --- | --- |
| A | `jobs.py:452` 的 `_TooLargeError` 分支先 `submit()` 一个合法 Job 再返回 400 | 用例 4 红 | 红：`_counts(db) == before` 失败，多张表出现 1 vs 0（`speech_jobs`/`speech_job_intents`/`speech_job_submissions`/`admission_operations`/`admission_requests` 等）；状态码/错误码仍 400 `invalid_job_request` | 零副作用断言有牙，作者第 3 条反例成立 |
| B | `public_jobs_postgres.py:191` 强制 `_admission_pool = None` 且 `_require_admission = True` | 成功用例红 | 红：`assert 503 == 202`，`job_unavailable` / "Job storage is unavailable." | 装配里的准入是真生效的 |
| C | `settings.py:174` 默认 `job_receive_max_bytes` 262144→524288 | 用例 4 红 | 红：`assert 300175 > 524288` | 场景前提守卫；建议注释说明 |
| D | `app.py:749` 装配点 `max_bytes` 改为 `1048576` | 若钉住接收上限则红 | **仍绿（1 passed）** | **用例 4 并不钉接收上限 → M1** |
| P | 判别探针：`text` 4096 码点 + 300000 B padding | 有上限 400 / 无上限 422 | 400 `invalid_job_request` / 422 `unsupported_parameter` | M1 的修法可行 |

变异 A/B/C/D 全部在 `/private/tmp/sn704-mut` 完成，实验后 `git checkout -- .` 还原并删除该 worktree 与分支；交付分支与远端没有任何变异。

---

## 本机复跑

**可以本机复跑，且已复跑**（不是拿 CI 当本机证据）：

```bash
# 一次性专用库；MySQL 用既有隔离容器（只连不启停）
createdb sn_r704_admission
SPEECHNEXUS_TEST_DATABASE_URL="postgresql://<user>@127.0.0.1:5432/sn_r704_admission" \
SPEECHNEXUS_TEST_MYSQL_URL="mysql://root:<password>@127.0.0.1:55434/" \
uv run --frozen pytest tests/integration/test_public_job_admission_http_pg.py -v
# → 5 passed in 1.73s（Python 3.14.9，pytest 9.1.1）
```

- 5 个用例逐个 `PASSED`，0 skipped。注意 `database` fixture 即使只选 `postgresql` 参数也要求两个 DSN 同时存在，缺任一 `pytest.fail`。
- 覆盖限制：只跑了本文件和 `test_pg_dsn_skip_guard.py`；未跑全量 `-m integration`（需要 MinIO 与更长时长），也没有跑作者列出的邻接套件 61 条。
- 结束后 `dropdb sn_r704_admission`；变异 worktree、分支、临时 ref 均已清理。

---

## 只能依赖作者陈述或未实测

- 作者 PR 描述里的"四条反例各自能让对应用例红"：我只独立复现了第 3 条（= 我的变异 A）。第 1、2 条可由我的探针推断（无名请求 → 503；4097 → 400），但没有逐条实跑；第 4 条（重放换新 key）未复现。
- 作者报的"邻接套件 `test_public_job_atomic_admission_pg.py` + `test_public_jobs_http.py` → 61 passed"未复跑（与本次结论无依赖）。
- 作者报的 `uv run mypy --platform linux`、`lint-imports`、`ruff check` 全绿：我只复跑了 `ruff format --check`（提交前 `1819 files already formatted`）与 `ruff check`（`All checks passed!`），mypy 与 lint-imports 未跑。
- CI 六分片全绿中与本文件无关的其余用例未逐条核对；我只核对了本文件 5 个用例的 `selected`/`outcomes` 与 `executed`。

---

## 校验记录

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 目标用例 | `uv run --frozen pytest tests/integration/test_public_job_admission_http_pg.py -v` | `5 passed in 1.73s` |
| 跳过守护 | `uv run --frozen pytest tests/integration/test_pg_dsn_skip_guard.py -q` | `15 passed in 0.90s` |
| 格式化 | `uv run --no-project ruff format --check .` | `1819 files already formatted` |
| 格式化（写模式） | `uv run --no-project ruff format .` | `1819 files left unchanged`（报告为 md，ruff 不改动它） |
| Lint | `uv run --no-project ruff check .` | `All checks passed!` |
| 边界 | `git show --stat 002c9a4b`、`git diff --name-only be8ac046...002c9a4b` | 被审 head 只有 `tests/integration/test_public_job_admission_http_pg.py`；交付分支只有本报告 |
| 锁文件 | `git status --porcelain`（提交前） | 空；`uv.lock` 未改 |
