---
title: "PR #839 第三轮（收口）非作者审查：readiness 错误码增量（#828）"
status: active
version: "0.1.0"
date: 2026-11-15
---

# PR #839 第三轮（收口）非作者审查：readiness 错误码增量（#828）

审查对象：PR [#839](https://github.com/hrygo/SpeechNexus/pull/839)
`fix/828-readiness-expiry`，head `11bf6e1bdbd81cf6bb8e9106d70acfe9d0bbe87a`（修 issue #828）。

**本卡只审第二轮之后的增量。** 前两轮报告已在 `origin/main`：

- 第一轮 [`2026-11-15-readiness-error-code.md`](2026-11-15-readiness-error-code.md)（head `956b43d4`，B1–B4）
- 第二轮 [`2026-11-15-readiness-error-code-rework.md`](2026-11-15-readiness-error-code-rework.md)（head `e5ef03f6`，M1–M5）

范围与基线说明（先说明为什么 baseline 变了）：

- 分支在第二轮之后 rebase 过，`origin/main` 从 `6a4d7184`（第二轮 merge-base）前进到
  `702e43f2`。第二轮的 `e5ef03f6` 在 rebase 后是 `e067ee27`（同一 commit message）。
  实测 `git diff 6a4d7184..702e43f2 --stat` = 136 文件 +4906/−162，
  `git diff e5ef03f6..e067ee27 --stat` = 同样 136 文件 +4906/−162，**逐文件同一个量**，
  即 `e067ee27` 的树 = `e5ef03f6` 的树 + 主干前进，被审内容没有被 rebase 改动。
- 因此本轮增量 = `e067ee27..11bf6e1b`（4 个 commit：`f55672b6` M1–M5 返修、
  `2cb03c8d` 存储层自我拒绝保持 `job_unavailable`、`52c6b7cf` 重出 validators、
  `11bf6e1b` 证据文档），**83 文件 +2258/−469**；其中产品代码只有 6 个 Python 文件、
  3 个契约文件、1 个 CI 检查脚本（+217 行）与 1 个新 CI 测试文件（120 行）。
- 审查分支 `review/852-readiness-deltas` 基于 `origin/main` = `702e43f2`，本报告是它与
  `origin/main` 的**唯一**差异（`git diff origin/main...HEAD --stat` 只有 1 文件）。

## 结论

**M2/M3/M4/M5 的自报基本落地且我独立复现；但 M1 只关死了三条通道里的两条半，
并且这一轮新加的两条「守护」各有一处实测假绿。**

逐条要点（细节见各节）：

1. **A1 三条通道**：`_raise_admission_rejection` 未登记码、voice-draft `KeyError`
   两条**确实**变成契约声明的 JSON 500 信封（`retryable: false`）。但第三条
   ——「码在词汇表、不在公开映射表」——**不是信封，是裸 500**
   （`content-type: text/plain`、`body='Internal Server Error'`）。原因：`rejection_code`
   在 `public_jobs_postgres.create` 的 `except AdmissionRejectedError` **处理器内部**抛出
   `PublicJobAdmissionRejectedError`，同 `try` 的兄弟 `except PublicJobAdmissionRejectedError`
   捕获不到，接口层也没有该类型的处理器。新增守护
   `test_an_unmapped_code_answers_the_declared_defect_envelope` 只断言 `rejection_code`
   抛出的**异常对象的 code**，从不跑 HTTP，所以对这一条全绿（§A1）。
2. **A2 单一来源成立**：`_PUBLIC_CODES is PUBLIC_ADMISSION_CODES` 实测同一对象；
   `_ADMISSION_REJECTION_CODES` 由映射**求值**得到。两个反例都真红：改回手写集合 → 1 failed、
   只改一个映射值而不动 `_ERRORS`/契约 → 3 failed（§A2）。
3. **A3 早退集合是新后门**：`_STORE_MECHANISM_CODES` 加一个码即可把**任意**未登记码还原成
   #828 的原响应 `503 job_unavailable` + "Job storage is unavailable."（实测原文），
   36 条守护全绿；而 `public_jobs.py:1507` 的注释声称
   `test_the_store_mechanism_codes_are_exactly_what_the_reason_mapping_declares` 钉住了它
   ——**该测试在全仓不存在**，四个机制码也没有任何测试引用（§A3）。
4. **B4 passthrough 白名单能假绿，且正是本卡预判的那点**：给已登记的
   `admit_in_transaction` passthrough 换一个不受限的 producer（`jobs_sql.py` 的
   `JobResult(error_code=<数据库读出的字符串>)`）→ **36 passed**。因为
   `test_the_jobs_write_passthrough_is_bounded_by_its_source` 只收集 `ast.Constant` 实参，
   非字面量被**静默跳过**而不是判红（§B4）。
5. **B5 清单今天仍是全集（13 = 13，全仓 grep 无第 14 个模块），但守护不能证明它**：
   新增第 14 个模块 raise 未登记码 → **36 passed**。`test_the_frozen_module_list_is_exactly_what_raises_rejection_errors`
   的 `scanned` 与 `declared` 都从 `ADMISSION_MODULES` 自身派生，`assert len(...) == 13` 是手写常数（§B5）。
6. **B6 两个泄漏码定案合理、且都已进词汇表与映射**：`unapproved_runtime_template`
   （`plan is None`，恢复=登记获准模板）→ `engine_resource_disabled`，同时把该码文案补成
   "…its pool, **its template** or its recipe approval…"；`gateway_drain_incomplete`
   （网关排水中）→ `job_admission_contended`（可重试）。两者都进了
   `ADMISSION_REJECTION_CODES` 与 `PUBLIC_ADMISSION_CODES`，所以
   `set(_PUBLIC_CODES) == ADMISSION_REJECTION_CODES - CAPACITY_REJECTION_CODES` 自然通过（§B6）。
7. **C8 六因实测与自报一致**；四个引擎原因合并成一个 `voice_draft_engine_unavailable`
   在「客户端动作相同」的口径下可接受，但与 jobs 面逐因发码**跨面不对称**（§C8）。
8. **C9 `job_request_expired` 留 422 正确**（该仓 422 的角色是「确定性拒绝、换新请求」，
   同 `workspace_not_configured` 先例），文案也不再归罪输入（§C9）。
9. **D10 `execution_cleanup_pending` false→true 安全**：仓库内没有任何消费方按
   `retryable` 自动重试 409（`file-task-session` 全程不读该字段、workbench 无重试循环），
   外部按契约读该位的客户端**会**重试——但重试对象是同一 Job 身份上的 cancel/retry，
   幂等、且状态自清；缺 `Retry-After` 是本次唯一实质隐患（§D10）。
10. **D11 存储拒绝全 false 无仓库内消费者依赖**，改后行为不变（§D11）。
11. **E12 会退回 413 文案——但是 `unreplayablePriorAttemptClause` 而不是 `unreplayableCause`**：
    `unreplayableCause` 的未知码回退是中性的（好），`unreplayablePriorAttemptClause` 的默认分支
    **写死 413**「请求体超过本部署的接收上限，但该意图此前已有提交尝试…」，任何未列出的码都会
    落到它（实测）。按本卡口径判阻塞（§E12）。
12. 例数与 CI 与自报一致（7203/7200/3330/3868/414、集成 208、workbench 1075、admin 151）；
    run `43721357511` 的 `head_sha` 实测 = `11bf6e1b`，23 success / 1 skipped / 0 failure（§F）。
13. 另发现：新 422 码 `voice_draft_request_expired` 在 `contracts/speech-openapi.yaml` 里
    **出现 0 次**（生成物同样 0 次），契约与生成的 TS 都无法发现它；五个 503 描述里还留了
    5 处 `--renew-readiness` **placeholder** 字样（§C8、§F15）。

## 核心四问

**1）三条通道真的关死了吗？——没有，两条关死、第三条仍是裸 500。**

| 通道 | 构造 | 实测最终响应 | 契约信封？ | `retryable` |
| --- | --- | --- | --- | --- |
| (a) `_raise_admission_rejection` 未登记码 | store 返回 `engine_retired` | `500 application/json` `admission_state_invalid` | **是** | `false` |
| (b) voice-draft submit 走 `KeyError` | store 抛 `KeyError("readiness_lapsed")` | `500 application/json` `voice_draft_contract_violation` | **是** | `false` |
| (c) 码只在词汇表、不在公开映射 | 真 PG + 生产装配，过期收据 | `500 text/plain` `Internal Server Error` | **否** | 无字段 |

(c) 就是第二轮 A2c 那条通道：伪装成存储故障的行为没了，但「契约新声明的 500 信封」**没有到来**。
作者自报「`job_errors.py` 未声明码改 `admission_state_invalid`（500）」对 `job_error()`
成立（§A1 的 A1.0 原文），但准入侧走的是 `rejection_code` → `PublicJobAdmissionRejectedError`，
没进 `job_error()`。

**信息泄漏判断**：`engine_retired` 这类内部码名**不构成需要处置的泄漏**——它不是凭据、不含
隐私、且这层已经写日志；但它确实把内部词汇暴露给调用方。实际发生的泄漏是**零**：
A1.2 的 message 是通用 `admission_state_invalid` 文案、不含码名；(c) 是裸 500、无 body。
唯一会带码名的是 `job_error()` 的未声明分支（§A1 A1.0），而它当前**不可达**——
`_raise_admission_rejection` 已先把它换成已声明码。判断：保留（利于排障），但别把它当
「已关死」的证据。

**2）passthrough 白名单能否假绿？——能，实测 36 passed 全绿。**

具体是 `allowed` 型的两条里，`admit_in_transaction`（`job.error_code or 'reconciliation_required'`）
那条：它的边界由 `jobs_sql.py` 的 `JobResult(error_code=...)` 字面量决定，而
`test_the_jobs_write_passthrough_is_bounded_by_its_source` 只把 `ast.Constant` 关键字收进
`emitted`，**非字面量既不进集合也不报错**。把其中一处换成 `error_code=cast(str, rows[0]["rejection"])`
（数据库读出的任意字符串）后守护全绿（§B4）。`producer` 型的三条反而是真的：
在 `owners.py` 里改一处动态传码 → 4 failed（因为 `OwnerRejectedError` 是被扫描的 producer，
动态 raise 点会被 `test_every_variable_passed_code_is_declared_as_a_passthrough` 抓住）。

**3）409 改 true 会不会引发自动重试？——仓库内不会，外部会；后果可控，但缺 `Retry-After`。**

- `execution_cleanup_pending` 只从 `_cancel`（`jobs.py:615`）与 `_retry`（`:640`）发出，
  create 路径发不出来，所以不会放大创建流量。
- workbench 的两个消费面都**不读** `retryable`：`file-task-session.ts:67` 明写
  「The client does not consume `error.retryable` at all」，`rejectionDisposition` 按
  `(status, code)` 精确表分类，409 走 `settlesCancelAttempt`；全仓 `web/` 也没有任何
  退避/自动重试循环（`grep 'for (let attempt|while (attempt|backoff'` 无命中）。
  只有 `voice-session.ts:214` 读 `retryable`，但那是 `unsupported_parameter` 422 分支。
- 外部按 `contracts/shared/errors.schema.json` 新规则读该位的客户端会重试 409。重试对象是
  同一 Job 身份上的 cancel / retry（后者带原 Idempotency-Key 时是重放），**不会重复执行**；
  拒绝写在前、状态自清（清理完成即放行），所以后果是「清理窗口内的请求放大」，
  不会幂等性破坏，也不会无限放大——**除非**客户端用紧退避：该 409 **没有** `Retry-After`
  （`job_errors.py` 只给 `job_capacity_exhausted` 加）。结论：**改 true 可以接受**
  （与 workbench 同情形 `render_submission_cleanup_pending` 的 `true` 对齐），建议补
  `Retry-After` 或把等待写进 description。

**4）`unreplayable` 有码会退回 413 文案吗？——会，在 `unreplayablePriorAttemptClause`。**

`unreplayableCause` 的未知码回退是中性的（`这次请求被协议确定性拒绝（${status} ${code}），原意图作废`），
但 `unreplayablePriorAttemptClause` 的默认分支是写死的 413 文案。实测：

```text
unreplayablePriorAttemptClause("admission_deadline_elapsed")
= 请求体超过本部署的接收上限，但该意图此前已有提交尝试，这次拒绝不能证明原提交未执行。
```

`file-task-rejection.test.ts` 新增的三条只 pin 了 `unreplayableCause` 的未知码与两个已知码，
**没有一条 pin `unreplayablePriorAttemptClause` 的未知码**，所以这个回退不会红。
今天 `unreplayable` 只被 `(413, job_request_too_large)` 与 `(422, job_request_expired)` 命中，
后者已分流，所以是**潜在**回退；按本卡口径（「若回退到 413 类断言，就是阻塞」）判阻塞（§E12）。

## 环境

| 项 | 值 |
| --- | --- |
| 主机 / 时间 | macOS arm64，2026-11-15（Asia/Shanghai），22:09 起 |
| 审查 worktree | `/private/tmp/sn852`，分支 `review/852-readiness-deltas`，基于 `origin/main` = `702e43f2` |
| 被审代码 worktree | `/private/tmp/sn852-run`，detached 于 `11bf6e1b`（只读上下文；变异只在此副本内，逐次 `git checkout --` 复原） |
| Python / pytest / pnpm | 3.14.9 / 9.1.1 / 12.14.0 |
| PostgreSQL | 一次性容器 `sn852-pg`（`postgres:16.15-alpine`，`127.0.0.1:55462`） |
| MySQL | 一次性容器 `sn852-mysql`（`mysql:8.4.8`，`127.0.0.1:55463`） |

两个容器与探针脚本在审查结束时清理；未触碰 `sn838-pg`、`sn824-pg`、`sn824-minio`、
`speechnexus-coordination-mysql`、`sn832-workbench-api-1` 等既有实例，未停止或修改任何共享 GPU
上的引擎，未 push main，未合并。

## 时间线（过程记录）

| 时刻 | 动作 | 结果 |
| --- | --- | --- |
| 22:09 | 建 `sn852`（基于 `origin/main`）与 `sn852-run`（detached `11bf6e1b`）；确认 `docs/reviews/2026-11-15-readiness-error-code-final.md` 不存在 | 文件不存在，可新建 |
| 22:10 | 读前两轮报告；核 `e5ef03f6`→`e067ee27` 的 rebase 等价性（`6a4d7184..702e43f2` 与 `e5ef03f6..e067ee27` 同为 136 文件 +4906/−162） | 增量 = `e067ee27..11bf6e1b`，83 文件 |
| 22:12 | 读 6 个产品文件与 3 个契约文件的增量；读新守护 `test_public_job_admission_error_vocabulary.py`（594 行） | 定位 `ADMISSION_MODULES`、`PASSTHROUGH_RAISE_POINTS`、`_STORE_MECHANISM_CODES` |
| 22:14 | 全仓 grep 三类拒绝错误的 raise 点 | 13 个模块，与清单一致；无第 14 个 |
| 22:16 | 发现 `public_jobs.py:1507` 引用的测试名全仓不存在 | 记为 A3 疑点 |
| 22:17 | `uv sync --frozen --all-packages`；起 `sn852-pg` / `sn852-mysql` | 环境就绪 |
| 22:19 | 单元探针（A1.2 / A1.3 / A3 / C8 十码） | 五段原文（§A1、§A3、§C8） |
| 22:21 | **A1.1** 集成探针（真 PG + 生产装配，`_PUBLIC_CODES.pop("readiness_expired")`） | `500 text/plain "Internal Server Error"` |
| 22:23 | A2 反例（改回手写接受集合） | `1 failed`（`test_the_acceptance_set_is_derived_from_the_mapping`） |
| 22:24 | A2b 反例（只改一个映射值） | `3 failed` |
| 22:25 | B4a 变异（`jobs_sql` 非字面量 producer）；B5 变异（新增第 14 个模块） | **各 36 passed（假绿）** |
| 22:26 | 复现作者三条反例 + 一个对照 | 3 failed / 1 failed / `assert 12 == 13` / 对照 4 failed |
| 22:28 | 全量 `tests/unit tests/contract tests/ci` | 首跑 `1 failed`（`test_production_owners`） |
| 22:29 | 定位该失败为**我自己变异残留的陈旧 `.pyc`**（变异与 `git checkout` 同秒、源码大小相同 → 字节码未失效） | 清 `__pycache__` 后 30 passed；重跑全量 |
| 22:30 | 六个集成文件（9+5+72+76+30+16 = 208） | `208 passed` |
| 22:31 | workbench / admin vitest | `1075 passed`（65 文件）、`151 passed`（19 文件） |
| 22:32 | `make contracts-check` / `contracts-drift` / `contracts-speech-changes` | 37 文件通过 / up to date / passed |
| 22:33 | E12 workbench 探针（未列出码） | `unreplayablePriorAttemptClause` 回退 413 文案 |
| 22:34 | 全量重跑 | `11482 passed, 3 skipped, 1424 subtests` |
| 22:35 | `gh api` run `43721357511` | `head_sha=11bf6e1b`，23 success / 1 skipped / 0 failure |
| 22:36 | 报告分支 `ruff format --check` | `1903 files already formatted` |

## A. M1 三条兜底通道（核心）

### A1 三条原始响应（全部本机实测）

探针放在 `sn852-run` 内的临时测试里（`raise_app_exceptions=False` 取最终响应），跑完删除。

**(a) `_raise_admission_rejection` 收到未登记码。** store 返回
`JobCreateResult("rejected", error_code="engine_retired")`，生产装配 HTTP `POST /v1/nexus/jobs`：

```text
status=500 content-type=application/json x-should-retry=None retry-after=None
body={"error":{"code":"admission_state_invalid","type":"api_error",
 "message":"Admission state failed an internal invariant; this is a deployment defect and
 the request must not be replayed as-is.","param":null,
 "request_id":"adfd675480f140d0a91235257da65917","retryable":false}}
```

直接调用同函数：`raises JobAdmissionRejectedError`，`code == "admission_state_invalid"`。
**信封成立、`retryable: false`、message 不含内部码名。**

**(b) voice-draft submit 走 `KeyError`。** store 的 `submit` 抛
`KeyError("readiness_lapsed")`，`POST /v1/nexus/voice-drafts/draft-1/validations`：

```text
status=500 content-type=application/json x-should-retry=false retry-after=None
body={"error":{"code":"voice_draft_contract_violation","type":"api_error",
 "message":"Admission state failed an internal invariant; this is a deployment defect.",
 "param":null,"request_id":"ea65088a04894c039fbbfca7998abab2","retryable":false}}
```

`except KeyError` 已从 `(…, KeyError, …)` 组里拆出来单列，落到
`_error("admission_defect")` → 500 `voice_draft_contract_violation`、`X-Should-Retry: false`。
**信封成立、`retryable: false`、不再说 storage。**

**(c) 码只在词汇表、不在公开映射（本卡要求的第一条）。** 真 PG 16.15 + 生产装配
（`create_app(settings, jobs=PublicJobs(store, store))`），把
`public_job_admission._PUBLIC_CODES`（与 `domain.admission.PUBLIC_ADMISSION_CODES` 同一对象）
里的 `readiness_expired` 摘掉、`ADMISSION_REJECTION_CODES` 保持含它，再把收据改成已过期后
`POST /v1/nexus/jobs`：

```text
status=500 content-type=text/plain; charset=utf-8 body='Internal Server Error'
```

**不是契约声明的 JSON 信封。** 机制：`rejection_code` 在
`public_jobs_postgres.py:593` 的 `except AdmissionRejectedError as error:` **处理器内部**被调用；
该处理器里抛出的 `PublicJobAdmissionRejectedError` 不会被同一个 `try` 的兄弟
`except PublicJobAdmissionRejectedError`（`:588`）捕获，继续上抛；而
`grep -rn "except PublicJobAdmissionRejectedError" backend/src` 只有 `public_job_admission.py:338`
与 `public_jobs_postgres.py:588`，接口层（`jobs.py` 只捕获 `JobAdmissionRejectedError`）没有它。
于是 Starlette 兜成裸 500。

**顺带一个对照**：`job_error()` 自己对未声明码的处理是达标的（**这是唯一会带内部码名的响应**）：

```text
500 application/json
{"error":{"code":"admission_state_invalid","type":"api_error",
 "message":"The Job API emitted an undeclared error code (engine_retired); this is a deployment
 defect and the request must not be replayed as-is.","param":null,
 "request_id":"rid-probe-und","retryable":false}}
```

但这条路径在准入侧**不可达**（`_raise_admission_rejection` 先把它换成已声明码），
真正走不到；能走到的是 (c) 的裸 500。所以「M1 把这类通道关成契约 500 信封」的结论只对
(a)(b) 成立。

**信息泄漏判断（本卡指定）**：三条实测响应里出现的内部码名数量是 **0**。A1.2 的 message 是
通用文案；(c) 无 body。`job_error()` 的未声明分支会把码名写进 message，但那是设计好的排障信息、
且当前不可达。判断：**不构成需要处置的泄漏**（不是凭据、不含隐私），但也不要用它当
「通道已关死」的证据——它连 HTTP 都没走到。

### A2 `PUBLIC_ADMISSION_CODES` 单一来源

**成立，两跳都是真的。** 实测：

```text
same object: True                       # public_job_admission._PUBLIC_CODES is domain.PUBLIC_ADMISSION_CODES
vocab size: 19  mapping size: 17        # 19 = 17 非容量 + 2 容量
values: admission_state_invalid, engine_not_ready, engine_resource_disabled,
        engine_state_mismatch, idempotency_conflict, job_admission_contended,
        job_request_expired, recipe_not_admitted   （8 个；+ job_capacity_exhausted = 9 个公开码）
```

HTTP 接受集合**是求值的**，没有第二份：

```python
_ADMISSION_REJECTION_CODES: frozenset[str] = (
    frozenset(PUBLIC_ADMISSION_CODES.values())
    | CAPACITY_REJECTION_CODES
    | {"job_capacity_exhausted"}
)
```

反例 1（改回手写集合、少一个值）——这条正是「只改映射不改接受集合」在今天能构造出的等价形态：

```text
FAILED tests/contract/test_public_job_admission_error_vocabulary.py::
       test_the_acceptance_set_is_derived_from_the_mapping
1 failed, 35 passed
```

反例 2（只改一个映射值，不动 `_ERRORS`/契约枚举）：

```text
FAILED ...::test_each_admission_cause_names_its_own_recovery[readiness_expired-engine_not_ready]
FAILED ...::test_every_mapped_public_code_is_declared_on_its_status
FAILED ...::test_no_store_refusal_returns_silently[engine_not_ready]
3 failed, 33 passed
```

即「映射 → 接受集合」与「映射 → 错误表/契约枚举」两跳都有真守护。

### A3 `_STORE_MECHANISM_CODES` 早退是不是新后门

**是。** 它确实堵住了第二轮 M1(b) 的「未命中也静默返回」，但换成了一个**新的、无守护的早退集合**：

```python
_STORE_MECHANISM_CODES = frozenset(
    {"job_rejected", "revision_exhausted", "job_identity_conflict", "storage_rejected"}
)
...
if code in _STORE_MECHANISM_CODES:
    return  # The caller raises the declared mechanism reason for this code.
```

`_CREATE_REFUSAL_CODES` 先判，所以**公开准入码塞进来无效**；但任何**不在**准入集合里的码
（例如 `engine_retired`）塞进来就会静默返回，调用点接着
`raise JobRejectedError(_rejection_reason(code))`，`_rejection_reason` 对未知码返回
`"contract"`，而 `"contract"` 在 HTTP 层就是 `job_error("job_unavailable")`。

实测（只把 `engine_retired` 并进该集合，其余不动，生产装配 HTTP POST）：

```text
status=503 content-type=application/json x-should-retry=None
body={"error":{"code":"job_unavailable","type":"service_unavailable_error",
 "message":"Job storage is unavailable.","param":null,
 "request_id":"ae5025a2ee284d62a9243b6d140ee075","retryable":false}}
```

**这就是 #828 的原响应。** 同时 36 条守护全绿。更关键的是
`public_jobs.py:1506-1509` 的注释写着

```text
# `test_the_store_mechanism_codes_are_exactly_what_the_reason_mapping_declares`
# pins the two together.
```

**该测试在整个仓库不存在**（`grep -rn "store_mechanism\|STORE_MECHANISM"` 只命中这处注释与定义；
`grep -n "storage_rejected\|job_rejected\|revision_exhausted\|job_identity_conflict" tests/unit/backend/test_public_jobs.py
tests/contract/test_public_job_admission_error_vocabulary.py` 无命中）。也就是说：注释声称的守护
不存在，四个机制码也没有任何测试覆盖，早退分支是纯靠人工自觉。

判断：**这是本轮新开的后门**，而且它打开的正是 M1 想关的那种「健康存储被答成存储故障」。
必须补双向等价测试（`_STORE_MECHANISM_CODES` ↔ `_rejection_reason` 的已声明分支），
并让注释指向真实存在的测试；更好的做法是从 `_rejection_reason` 的分支派生该集合。

## B. M2 AST 守护能否再被绕过（重点）

基线：`tests/contract/test_public_job_admission_error_vocabulary.py` 36 passed。

### B4 `PASSTHROUGH_RAISE_POINTS` 白名单（本轮最可能的假绿点，预判成立）

5 条登记项：

| # | 站点 | 边界类型 | 边界是否真的被断言 |
| --- | --- | --- | --- |
| 1 | `domain/admission.py:require_live_authority` `error.code`（`OwnerRejectedError`） | `producer` | **是**：`owners.py` 被扫描，动态 raise 会红（对照实测 4 failed） |
| 2 | `domain/admission.py:require_live_authority` `error.code`（`RuntimeRejectedError`） | `producer` | **是**：runtime 模块被扫描 |
| 3 | `infrastructure/admission_sql.py:_runtime` `error.code`（`RuntimeRejectedError`） | `producer` | **是**：同上 |
| 4 | `infrastructure/admission_sql.py:admit_in_transaction` `job.error_code or 'reconciliation_required'` | `allowed` | **否（假绿）** |
| 5 | `domain/runtime_observations.py:require_valid` `expired_code` | `allowed` | 部分：默认值与扫描模块内的 `expired_code=` 调用点被 pin；**扫描范围外的新调用点看不见** |

第 4 条的假绿实测：把 `jobs_sql.py` 的
`JobResult("conflict", …, error_code="idempotency_conflict")` 改成
`JobResult("conflict", …, error_code=cast(str, rows[0]["rejection"]))`（`rows` 来自
`cursor.fetchall()`，即数据库读出的任意字符串）：

```text
....................................  [100%]
36 passed in 0.15s
```

原因在 `test_the_jobs_write_passthrough_is_bounded_by_its_source`：

```python
for keyword in node.keywords:
    if keyword.arg == "error_code" and isinstance(keyword.value, ast.Constant):
        emitted.add(keyword.value.value)
```

非 `Constant` 的实参**既不进 `emitted` 也不进 `offenders`**，于是「边界」只对字面量成立。
这正是本卡预判的「白名单一登记就永远通过」的形态。

对照（证明 `producer` 型不是纸糊的）：把 `owners.py` 的
`raise OwnerRejectedError("owner_busy")` 改成 `raise OwnerRejectedError(_row["code"])`：

```text
FAILED ...::test_every_raise_point_contributes_only_declared_codes
FAILED ...::test_every_variable_passed_code_is_declared_as_a_passthrough
FAILED ...::test_the_producer_bounded_passthroughs_stay_inside_the_vocabulary
FAILED ...::test_the_vocabulary_is_exactly_what_the_raise_points_carry
4 failed, 32 passed
```

第 5 条的残余：`test_the_declared_expired_code_is_the_only_bound_on_the_receipt_wrapper` 只遍历
`ADMISSION_MODULES`，所以**扫描范围外的新调用点**（例如未来在 `engine_registration` 里再调一次
`require_valid(..., expired_code=<变量>)`）看不见。今天 `require_valid` 只有 2 个调用点
（`domain/admission.py:413` 带字面量、`domain/runtime_observations.py:458` 用默认值），
所以在编状态是好的。

### B5 13 模块清单是否仍是全集

**今天仍是全集，但守护不能证明它。**

独立 grep（全仓、排除 tests）三类错误的 raise 点：

```text
  27 backend/src/speechnexus_backend/speech/domain/runtime_intents.py
  26 .../speech/domain/runtime_observations.py
  23 .../speech/infrastructure/runtime_sql.py
  21 .../speech/domain/admission.py
  13 .../speech/infrastructure/admission_recovery_sql.py
  12 .../speech/infrastructure/admission_sql.py
  11 .../speech/infrastructure/qualification_admission_postgres.py
  10 .../speech/domain/owners.py
   9 .../speech/infrastructure/runtime_observation_sql.py
   6 .../speech/infrastructure/owners_sql.py
   6 .../speech/infrastructure/admission_postgres.py
   3 .../speech/infrastructure/tts_settlement_sql.py
   3 .../speech/infrastructure/builtin_voice_qualification.py
```

13 个，与 `ADMISSION_MODULES` 逐一对应，**没有第 14 个**。
（另外 `application/public_jobs.py` 与 `infrastructure/public_job_admission.py` 也构造
`*RejectedError`，但那是 `JobAdmissionRejectedError` / `PublicJobAdmissionRejectedError`，
不是被扫描的三类；升级到「构造点」口径会误报，这点清单口径没问题。）

但守护的强度不够。新增一个第 14 个模块：

```python
# backend/src/speechnexus_backend/speech/domain/runtime_quota_checks.py
def check_quota_band(band: str) -> None:
    raise RuntimeRejectedError("quota_band_unknown")
```

```text
....................................  [100%]
36 passed in 0.15s
```

因为 `test_the_frozen_module_list_is_exactly_what_raises_rejection_errors` 的
`scanned = {point.module for point in ALL_RAISE_POINTS}` 与
`declared = [module.split(...) for module in ADMISSION_MODULES]` **都来自同一个手写清单**，
再加一句 `assert len(ADMISSION_MODULES) == 13`。它只能发现「清单被删了一行」，
发现不了「仓库里多了一个 raise 模块」。测试名「is exactly what raises rejection errors」
因此是过度声明。

### B6 两个泄漏码的定案

| 内部码 | 公开码 | 语义判断 | 是否进词汇表 | 是否进映射 |
| --- | --- | --- | --- | --- |
| `unapproved_runtime_template` | `engine_resource_disabled` | **对**：`runtime_intents.py:362` / `runtime_sql.py:495` 都是 `plan is None`（请求不是从获准 plan 建的），恢复=登记/获准模板，属部署侧不可用 | 是（`ADMISSION_REJECTION_CODES`） | 是 |
| `gateway_drain_incomplete` | `job_admission_contended` | **大体对**：网关仍在排空上一代工作、`all_*_terminal` 未满足，排空会自行完成，属「同一请求稍后可成」 | 是 | 是 |

两点补充判断：

1. `engine_resource_disabled` 的文案本轮**同步**补了 "…its pool, **its template** or its recipe
   approval…"，与 `unapproved_runtime_template` 的来路对得上，不是只加码不改文案。
2. `gateway_drain_incomplete` 有一处边界：`runtime_intents.py:462` 是
   `if not prerequisites.all_leases_released and not prerequisites.fenced_quarantine_cleanup`
   ——租约未全释放**且**没有 fencing 清理时，这不一定是「会自清」，可能是死住需要运维。
   把它一律归到 `retryable: true` 的 `job_admission_contended` 在这种卡死形态下会诱导无效重试。
   今天它只在网关退役/停止流程里出现、不在 create 路径上，所以是潜在问题而非现行缺陷
   （建议修，见 S2）。

关于「若没补进 `ADMISSION_REJECTION_CODES`，`_PUBLIC_CODES` 完备性测试怎么过」：
两个码**都补了**，所以 `assert set(_PUBLIC_CODES) == ADMISSION_REJECTION_CODES - CAPACITY_REJECTION_CODES`
自然成立（19 − 2 = 17 = 映射条数，实测）。这条不是绕过，是真补。

### B7 复现作者自报的三条反例

| 反例 | 自报 | 实测 |
| --- | --- | --- |
| 未扫描/扫描模块加未登记码 | 3 failed | **3 failed**（`test_every_raise_point_contributes_only_declared_codes`、`test_the_producer_bounded_passthroughs_stay_inside_the_vocabulary`、`test_the_vocabulary_is_exactly_what_the_raise_points_carry`） |
| 变量传码未登记 | failed | **1 failed**（`test_every_variable_passed_code_is_declared_as_a_passthrough`，报 `domain/admission.py:decide_admission:448 (_RECIPE_REFUSAL)`） |
| 从 `ADMISSION_MODULES` 删模块 | `assert 12 == 13` failed | **`AssertionError: assert 12 == 13`**（`test_the_frozen_module_list_is_exactly_what_raises_rejection_errors`） |

三条都真红，与自报一致。但它们只覆盖「清单内部一致性」；结合 B4/B5，清单**之外**的
两处仍是绿的。

## C. M3 voice-draft 六因（判断）

### C8 六因实测

`validateVoiceDraftRevision`（生产 HTTP 装配，注入各码）：

```text
engine_not_ready        503 voice_draft_engine_unavailable  retryable=False  "The pinned engine's readiness receipt expired; re-register the engine (speechnexus-register-engine --renew-readiness), then resubmit."
engine_state_mismatch   503 voice_draft_engine_unavailable  retryable=False  "The pinned engine no longer matches this request's runtime state; re-register the engine, then resubmit."
engine_resource_disabled 503 voice_draft_engine_unavailable retryable=False  "The pinned engine instance, its pool, its template or its recipe approval is not active; restore it, then resubmit."
recipe_not_admitted     503 voice_draft_engine_unavailable  retryable=False  "The request does not match the recipe approved for the pinned engine; register the engine against the approved recipe, then resubmit."
job_request_expired     422 voice_draft_request_expired     retryable=False  "The admission deadline for this request elapsed before it was admitted; submit a new request."
admission_state_invalid 500 voice_draft_contract_violation  retryable=False  "Admission state failed an internal invariant; this is a deployment defect."
job_admission_contended 503 voice_draft_admission_contended retryable=True  "Admission state has not settled; retry the request."
workspace_not_configured 422 voice_draft_input_unavailable  retryable=False  "Published model or ready input required."
unsupported_parameter   422 voice_draft_input_unavailable   retryable=False  "Published model or ready input required."
engine_retired（未登记） 500 voice_draft_contract_violation  retryable=False  "Admission state failed an internal invariant; this is a deployment defect."
```

六因分流与自报完全一致：四个引擎原因各有不同 message 且都点名恢复步骤、`retryable: false`、
`X-Should-Retry: false`；`job_request_expired` 独立 422；`admission_state_invalid` 500；
竞争 503 且不再提 storage。

**判断一：四个引擎原因合并成一个 `voice_draft_engine_unavailable` 可接受。** 理由：

1. 四者对**客户端**的区别为零——同一个状态类（503）、同一个 `retryable: false`、同一个
   「别重放、去报告」动作，差别只在**运维**该做什么，而运维读的是 message，不是 code。
2. 该面此前的失败模式不是「码太粗」而是「归因错」（把引擎侧原因说成 422 输入错 /
   存储故障）。这轮改的是归因，不是粒度；粒度在这里不产生可执行差异。
3. 契约把四个原因逐条写进了 `voice_draft_engine_unavailable` 的 description，
   机器可读的部分（status/retryable/枚举）已正确。

**但有一个跨面不对称，值得记一笔（建议修，非阻塞）**：同样的四个原因此时在 jobs 面
（`createJob` 503 枚举）是**四个独立码**（`engine_not_ready` / `engine_state_mismatch` /
`engine_resource_disabled` / `recipe_not_admitted`），在 voice-draft 面是一个码。
一个同时消费两个面的网关要么按面分别写表、要么会以为 voice-draft 面「分不出来」。
保留单码可以，但应该在 `contracts/speech-openapi.yaml` 的对应 503 description 里写明
「本码在 jobs 面对应 engine_not_ready/engine_state_mismatch/engine_resource_disabled/recipe_not_admitted」，
让两个面的映射是**可查的**而不是只在本报告里成立。

**必须一并记的问题（见 F6）**：`voice_draft_request_expired` 在
`contracts/speech-openapi.yaml` 里出现 **0 次**（`grep -c` 实测 0，生成的
`packages/contracts/src/speech.generated.ts`、`speechnexus_contracts/voice_drafts.openapi.json`
同样 0 次）。后端会发它（上面实测），契约与生成物都不知道它存在。
`_error()` 之所以不报错，是因为这五个提交操作的 422 走共享 `UnprocessableEntity` 组件
（`code` 是无约束字符串）。`voice_draft_admission_contended` / `voice_draft_engine_unavailable`
各出现 15 次（5 个提交操作的枚举 + 描述），说明这轮确实做了枚举——**422 这一支漏了**。
`voice_draft_input_unavailable` 同样是 0 次，属既有缺口（本轮沿用），不是本轮引入。

### C9 `job_request_expired` 留在 422 是否正确

**正确，但它的正确性来自「本仓 422 的角色」而不是「输入错误」。**

- 从语义上看它**不是**输入错误：截止时间（`deadline_at_us`）是准入事务里服务器侧钉进 intent 的，
  调用方改请求体也改不了它；真实恢复是「提交新请求」。
- 但本仓 422 一直承担「**确定性拒绝、同一请求不再接受、换新请求**」这一角色，
  包括需要**带外**修复的 `workspace_not_configured`（"provision the quota"）也是 422。
  在这个既有约定下，`job_request_expired` 留 422 **与 jobs 面一致**（jobs 面同一条件也是
  `422 job_request_expired`），跨面一致性成立。
- 关键在文案：现在的 message 是 "The admission deadline for this request elapsed before it was
  admitted; submit a new request." ——陈述事实与下一步，没有归罪请求内容。这比第二轮
  「`422 voice_draft_input_unavailable` + Published model or ready input required.」准确得多。
- 唯一残余风险在**契约层**：这五个操作的 422 没有收窄枚举，客户端无法从 schema 发现
  `voice_draft_request_expired`（即 F6）。这是声明缺口，不是状态码选错。

判断：**保留 422，判定正确**，但必须把这个码写进契约（F6）。

## D. M5 `retryable` 取值变更的兼容性

### D10 `execution_cleanup_pending` 409 从 `false` 改 `true`

**有没有客户端会因此自动重试一个 409：仓库内没有，外部按契约会。**

仓库内消费面（逐条核到代码）：

| 位置 | 是否读 `retryable` | 对 409 的处置 |
| --- | --- | --- |
| `web/workbench/src/file-workflow/file-task-session.ts`（唯一 createJob 调用点的拒绝表） | **不读**（`:67` 明写 "The client does not consume `error.retryable` at all"） | 按 `(status, code)` 精确表：create 面 409 `execution_cleanup_pending` → `unknown-outcome`（`:180`）；cancel 面 → `settlesCancelAttempt`（`:307-308`） |
| `web/workbench/src/recovery/command-journal.ts` `refusedCategory/refusedReplayable` | 读，但入参是 **workbench 面** 的 `WorkbenchResponseError.retryable` | 与 speech Job 409 无关 |
| `web/workbench/src/assistant/assistant-session.ts:260` | 同上（workbench 面） | 无关 |
| `web/workbench/src/voices/voice-session.ts:214` | 读，但只在 `unsupported_parameter` 422 分支 | 无关 |
| `web/admin` | 无 createJob、无 retryable 分支 | 无关 |
| `packages/contracts` | 只生成类型，无重试逻辑 | 无关 |

全仓也没有自动重试循环：`grep -rn "for (let attempt|while (attempt|backoff" web/ --include=*.ts`
无命中；`X-Should-Retry` 只有后端在写、没有客户端在读（唯一的读法出现在后端注释里）。

外部：`contracts/shared/errors.schema.json` 明写「a caller reads this field instead of inferring
retryability from the status」，所以按契约实现的外部 SDK/网关**会**因为这一位改变行为：
从「不重试、等人工」变成「可重试」。

**后果判断：可接受。**
- 发出点只有 `_cancel`（`jobs.py:615`）与 `_retry`（`:640`），create 路径发不出来，
  所以不会放大创建吞吐。
- 两者都是**同一 Job 身份**上的操作：cancel 是对已存在 Job 的状态迁移，retry 在
  「父执行未确认停止」时被拒在写之前；带原 Idempotency-Key 的重放是恢复而不是新建。
  所以**不会重复执行、不会多建 Job**。
- 状态自清：清理一完成同一请求即被接受，重试窗口 = 清理时长（通常远短于 TTL）。
- **唯一实质隐患**：该 409 不带 `Retry-After`（`job_errors.py` 只给
  `job_capacity_exhausted` 加），紧退避的客户端会在清理窗口内持续打 cancel/retry。
  建议补 `Retry-After`（等于清理轮询间隔）或把等待写进 description（S1）。

结论：**行为变更成立且不破坏幂等**，与 workbench 面同情形
`render_submission_cleanup_pending`（`true`）对齐；请作为对外行为变更在证据文档里点名，
并补 `Retry-After`。

### D11 存储拒绝全改 `false`

改动点：`job_errors.job_unavailable`（本增量未触碰，仍是 false）、`voice_drafts.unavailable`
（`true → false`）、`voice_publications.unavailable`（`true → false`）。

**没有任何仓库内消费者依赖「503 存储 = 可重试」来驱动重试。** 逐条核：

- `web/workbench/src/voices/voice-session.ts`：`retryStorage()`（`:133`）重试的是**本地
  journal（localStorage CAS）写入**，不是 HTTP 503；调它的按钮 `VoiceCreateHost.tsx:221`
  不受 `error.retryable` 控制。收到 503 时走 `#mutationError` 的通用分支 → `outcome_unknown`
  / `input_or_reference_invalid`，与 `retryable` 无关。
- `web/workbench/src/file-workflow/*`：不读 `retryable`（同上）。
- `web/admin`：不消费 voice-draft 面。
- 契约层：`X-Should-Retry` 从 `const: "true"` 放宽成 `enum ["true","false"]`，
  生成 validator 同步；没有消费者把 `X-Should-Retry == "true"` 当断言。

改后行为：存储拒绝不再对外声明「可重试」。语义上这与 jobs 面 `job_unavailable` 一致，
也与新规则「`true` = 无需带外改动即可成功」自洽（一次被拒的存储读写，重复它得到的还是同一拒绝）。
**判断：安全，且一致性是净收益。** 一个可讨论的细节：数据库瞬时故障（重连后读成功）
严格说也不需要运维动作，按新规则更接近 `true`；但 `job_unavailable` 历来是 `false` 且
description 明确「5xx 陈述的是未知结果而不是 can-retry 许可」，所以对齐既有约定更好。
非阻塞记录（S3）。

## E. M4 文案

### E12 分支覆盖与未列出码的输出

实现（`web/workbench/src/file-workflow/file-task-session.ts:141-153`）：

```ts
export function unreplayableCause(status: number, code: string): string {
  if (code === "job_request_expired") return "该请求自身的准入期限在获准前已过，这次请求被拒绝、原意图作废";
  if (status === 413) return "请求体超过本部署的接收上限，这次请求被拒绝、原意图作废";
  return `这次请求被协议确定性拒绝（${status} ${code}），原意图作废`;
}
export function unreplayablePriorAttemptClause(code: string): string {
  if (code === "job_request_expired") return "该请求自身的准入期限在获准前已过，但该意图此前已有提交尝试，这次拒绝不能证明原提交未执行。";
  return "请求体超过本部署的接收上限，但该意图此前已有提交尝试，这次拒绝不能证明原提交未执行。";
}
```

构造未列出的码（vitest 实跑）：

```text
[E12 422 admission_deadline_elapsed] cause=这次请求被协议确定性拒绝（422 admission_deadline_elapsed），原意图作废
                                     || prior=请求体超过本部署的接收上限，但该意图此前已有提交尝试，这次拒绝不能证明原提交未执行。
[E12 413 admission_deadline_elapsed] cause=请求体超过本部署的接收上限，这次请求被拒绝、原意图作废
                                     || prior=请求体超过本部署的接收上限，但该意图此前已有提交尝试，这次拒绝不能证明原提交未执行。
```

结论：

1. `unreplayableCause` 的未知码回退**中性**（还带上 status/code 便于排障），不退回 413 断言；
   唯一的边界是「未知码配 413 状态」仍走 413 文案——413 本来就是体积上限，可接受。
2. `unreplayablePriorAttemptClause` 的未知码回退**就是 413 断言**，即本卡判据里的阻塞项。
3. 测试缺口：`file-task-rejection.test.ts:1298-1318` 只 pin 了
   `unreplayableCause` 的未知码、两个已知码，以及 disposition 归属；
   **没有任何一条 pin `unreplayablePriorAttemptClause` 的未知码**，所以这个回退不会红。
4. 已改好的部分确认：`FileTaskPanel.tsx:125` 与 `file-task-session.ts:627` 的恢复态文案
   已从「原请求因请求体超过…」改成中性「原请求已被协议拒绝…/协议已拒绝这次请求且不能按原键再次提交」，
   `tests/web/file-task.behavior.spec.mjs` 与 `FileTaskHost.test.tsx` 的浏览器用例同步改了 pin；
   `FileTaskHost.test.tsx` 新增的用例真跑 422 `job_request_expired` 全链并断言
   「不出现『请求体超过本部署的接收上限』」——这是本轮最扎实的一条 M4 证据。

## F. 例数与 CI

### F13 例数（全部本机实测，head `11bf6e1b`）

| 套件 | 实测 | 自报 | 一致 |
| --- | --- | --- | --- |
| `tests/unit` | 收集 **7203**，`7200 passed, 3 skipped` | 7203 / 7200 + 3 skipped | ✓ |
| `tests/unit/backend` | `3330 passed` | 3330 | ✓ |
| `tests/contract` | `3868 passed` | 3868 | ✓ |
| `tests/ci` | `414 passed` | 414（1418 subtests） | ✓ |
| 三者合并 | `11482 passed, 3 skipped, 1424 subtests passed`（286.46s） | 11482 / 3 / 1424 | ✓ |
| 集成 6 文件（`test_public_job_admission_http_pg` 9 + `test_engine_registration_pg` 5 + `test_production_admission` 72 + `test_voice_drafts_pg` 76 + `test_public_jobs_http` 30 + `test_public_job_atomic_admission_pg` 16） | `208 passed`（93.59s） | 208 | ✓ |
| `pnpm test` workbench | `1075 passed`（65 文件） | 1075 | ✓ |
| `pnpm test` admin | `151 passed`（19 文件） | 151 | ✓ |
| `make contracts-check` | `contract validation: passed (37 files)` | 同 | ✓ |
| `make contracts-drift` | `Found 36 errors (36 fixed, 0 remaining).` / `Contract bindings: up to date` | 同 | ✓ |
| `CONTRACT_BASE_REF=origin/main make contracts-speech-changes` | `Known HTTP structural checks: passed` | 同 | ✓ |
| `uv run --no-project ruff format --check .`（报告分支，含本报告） | `1904 files already formatted` | 1905（其证据文档时点） | 时点差异 |
| `scripts/dev-env/check-http-contract-changes.py` 的批准表 | **27 条** `_APPROVED_RESPONSE_VOCABULARY_WIDENINGS` | 「27 条词汇放宽」 | ✓ |

一个过程记录（不影响结论）：首次全量跑出现 `1 failed`
（`tests/unit/test_production_owners.py::test_owner_versions_expiry_and_counter_overflow_cannot_silently_wrap`，
`NameError: name '_row'`）。定位为**我自己 B7 对照变异留下的陈旧字节码**：变异
（`raise OwnerRejectedError(_row["code"])`）与原行
（`raise OwnerRejectedError("owner_busy")`）**长度相同（8976 字节）**、`git checkout` 与编译落在
**同一秒**，于是 `.pyc` 头里的 `(mtime, size)` 与复原后的源码吻合、未失效。清 `__pycache__`
后该文件 `30 passed`，全量重跑 `11482 passed, 3 skipped`。**与被审代码无关**，
但说明了「逐次 `git checkout` 变异」的一个真实陷阱，记录在此。

### F14 `gh api` 直查 run

```text
$ gh api /repos/hrygo/SpeechNexus/actions/runs/43721357511 --jq '{head_sha,head_branch,conclusion,event,status}'
{"conclusion":"success","event":"pull_request","head_branch":"fix/828-readiness-expiry",
 "head_sha":"11bf6e1bdbd81cf6bb8e9106d70acfe9d0bbe87a","status":"completed"}

$ gh api .../runs/43721357511/jobs --paginate --jq '[.jobs[]|.conclusion] | group_by(.) | map({(.[0]): length}) | add'
{"skipped":1,"success":23}
```

`head_sha` 与 `11bf6e1b` 一致；**23 success / 1 skipped / 0 failure**。skipped 是
`Docs: markdown code block formatting`（既有条件分支）。

### F15 本轮新增的一个 CI 守护（顺带审）

增量在既有的 `scripts/dev-env/check-http-contract-changes.py`（1014 → 1231 行）里加了
`_APPROVED_RESPONSE_VOCABULARY_WIDENINGS`（27 条），并新增
`tests/contract/test_approved_http_response_vocabulary_changes.py`（120 行）。判断：**机制合格**——
键是「文件:JSON Pointer:变更种类」的精确行、带 `issue`/`reason`/`from_version`/`to_version`，
且测试证明「每条批准都对应真实比对产出的 finding」「未登记放宽仍会被拒」「只在其自身来源与
版本对上生效」。它确实让本 PR 的 27 处 503 词汇放宽变得可审（每处都有理由），
不是无脑放行。唯一提醒：`from_version` 与 `to_version` 都是 `0.1.0.dev18`（同一版本），
版本位对这批放宽没有区分力，靠的是精确键本身。

## 必须修清单

**F1（阻塞，核心）「词汇表有、公开映射没有」这条通道仍是裸 500，不是契约 500 信封。**
实测（真 PG + 生产装配）：`500 text/plain` `Internal Server Error`。
成因：`rejection_code` 在 `public_jobs_postgres.py:593` 的 `except AdmissionRejectedError`
**处理器内部**抛 `PublicJobAdmissionRejectedError`，同一 `try` 的兄弟处理器（`:588`）捕不到，
接口层也没有该类型的 handler。
修法（任选其一，改动很小）：把 `rejection_code(error.code)` 的求值挪到 `except` 外
（先算 code，再 `raise`）、或在 `create` 外层再包一层
`except PublicJobAdmissionRejectedError as error: raise _AdmissionRefusedError(error.code)`、
或在 `PublicJobs.submit` 统一转成 `JobAdmissionRejectedError`。
**并补一条真 PG + 生产 HTTP 的用例**，断言它是 `application/json` +
`{"error":{"code":"admission_state_invalid", …, "retryable": false}}`；现在的
`test_an_unmapped_code_answers_the_declared_defect_envelope` 只断言异常对象，证明不了 HTTP。

**F2（阻塞，核心）`_STORE_MECHANISM_CODES` 早退是新的无守护后门，且注释指向不存在的测试。**
实测：把 `engine_retired` 并进该集合 → `503 job_unavailable` + "Job storage is unavailable."
（#828 原响应），36 条守护全绿。
`public_jobs.py:1506-1509` 注释声称的
`test_the_store_mechanism_codes_are_exactly_what_the_reason_mapping_declares` **不存在**，
四个机制码也没有任何测试引用。
修法：补该测试（`_STORE_MECHANISM_CODES` ↔ `_rejection_reason` 的已声明分支**双向等价**），
并让早退**只能**覆盖这四个码（从 `_rejection_reason` 派生集合）；注释改为指向真实测试。

**F3（阻塞）`admit_in_transaction` 这条 passthrough 的边界是假绿。**
实测：`jobs_sql.py` 的 `JobResult(error_code=...)` 换成数据库读出的变量 → 36 passed。
修法：`test_the_jobs_write_passthrough_is_bounded_by_its_source` 要把**任何**非 `ast.Constant`
的 `error_code` 实参判为 offender（而不是静默跳过），并覆盖 `error_code` 的赋值式写法；
必要时把扫描扩到整个函数体内的 `error_code` 绑定。

**F4（阻塞）13 模块清单不可由守护证明完整性。**
实测：新增第 14 个模块 raise 未登记码 → 36 passed。今天清单**是**完整的（全仓 grep 13 = 13），
但 `test_the_frozen_module_list_is_exactly_what_raises_rejection_errors` 的
`scanned`/`declared` 同源，`assert len(...) == 13` 是手写常数。
修法：模块集合改为**全仓扫描**（例如扫 `backend/src` 下所有 `.py` 里对三类错误的构造/raise）
再与冻结清单比对，二者不等即红；至少把该测试名改为它真正证明的东西。

**F5（阻塞）`unreplayablePriorAttemptClause` 对未列出的码回退 413 文案。**
实测：`unreplayablePriorAttemptClause("admission_deadline_elapsed")` →
「请求体超过本部署的接收上限，但该意图此前已有提交尝试…」。
修法：默认分支改成中性（与 `unreplayableCause` 的
`这次请求被协议确定性拒绝（${status} ${code}），原意图作废` 同义），
并在 `file-task-rejection.test.ts` 补一条**未列出码**的 pin（现在只 pin 了 `unreplayableCause` 的）。

**F6（小改动）新 422 码 `voice_draft_request_expired` 在契约与生成物里都不存在。**
实测 `grep -c voice_draft_request_expired contracts/speech-openapi.yaml` = **0**，
`speech.generated.ts` = 0，`voice_drafts.openapi.json` = 0；而后端会发它（§C8 实测）。
`_error()` 不报错是因为五个提交操作的 422 走无约束的共享 `UnprocessableEntity`。
修法：至少把该码（以及既有的 `voice_draft_input_unavailable`）写进五个提交操作 422 的
description；若要机器可读则给 422 加 `allOf` 收窄（做法与 503 相同）。跑
`make contracts-generate` + `make contracts-drift`。

**F7（小改动）契约 description 里残留 5 处 `--renew-readiness` placeholder 字样。**
`contracts/speech-openapi.yaml:1890,1997,2292,2402,2512` 都是
「an expired readiness receipt (re-register with `--renew-readiness` placeholder)」，
明显是未完成的编辑。删掉 `placeholder` 一词即可（改契约文本需重出生成物 + drift）。

## 建议修（非阻塞）

- **S1** `execution_cleanup_pending`（409，现 `retryable: true`）补 `Retry-After`，或把
  「清理完成后同一请求会被接受」写进 409 的 description。当前按契约读该位的外部客户端
  会在清理窗口内紧退避重试（§D10）。
- **S2** `gateway_drain_incomplete` 的有一条边界是「租约未全释放**且**无 fencing 清理」
  （`runtime_intents.py:462`），那可能是需要运维的死住态，而它现在统一映射到
  `retryable: true` 的 `job_admission_contended`。今天不在 create 路径上，建议在该映射的
  注释里写明「仅当排空确实在推进时成立」，或把该分支单列。
- **S3** `voice_draft_unavailable` 改为 `false` 之后，description 的
  「the store refused this dispatch, so repeating it is answered by the same refusal」
  对**瞬时**存储故障（重连后成功）不完全准确；与 `job_unavailable` 对齐是净收益，但值得
  在该 description 里补一句「a recovered store changes the answer」。
- **S4** voice-draft 面把四个引擎原因合成一个码，与 jobs 面四个独立码不同构；
  建议在契约 description 里写明两个面的对应关系，让映射可查（§C8）。
- **S5** `require_valid(..., expired_code=...)` 的边界断言只遍历 `ADMISSION_MODULES`；
  扫描范围外的新调用点不可见（今天只有 2 个调用点、都在范围内）。建议把该断言改成
  全仓扫描 `require_valid(` 调用点。
- **S6** 第一轮 §B8 的「`/readyz` 不因收据过期失败、但参考部署没有**任何**自动信号」本轮未处理，
  也不在本轮增量范围内（issue #840 类），保留跟踪。
- **S7** `test_the_narrowed_statuses_do_not_advertise_codes_they_cannot_return` 仍只证明
  「跨操作状态一致、且码在错误表里的状态对得上」（即第二轮报告结论第 5 点指出的强度不足），
  本轮未加强；建议按「枚举码在 `createJob` 处理器里有 emit 点」的静态检查补强。

## 只能依赖作者陈述的点

| 事项 | 原因 |
| --- | --- |
| 另外四个提交操作（`createVoiceDesign` / `createVoiceClone` / `generateVoiceDraftRevision` / `createVoicePreview`）的六因实测响应 | 我只驱动了 `validateVoiceDraftRevision`；其余四个走同一 `_execute` 分支与同一 `_ERRORS` 表，代码路径同构，但我没有逐个发请求 |
| 真实引擎的 `/health` 探测与 `--renew-readiness` 对真实端点的效果 | 本机没有真实引擎，按项目约定不用 fake 代替；本轮未重新验证（第二轮已核事务与 `verified_at_us` 递增路径） |
| `docker compose --profile engine-readiness run --rm …` 的实际容器启动 | 未构建镜像跑容器；只核了 Compose 可解析、`tests/ci` 文本守卫与 wiring |
| 远端 CI 八个 Integration 分片的**内部**断言 | 我只核 run 的 `head_sha`、结论与 job 计次，未下载逐分片日志 |
| `_APPROVED_RESPONSE_VOCABULARY_WIDENINGS` 里 `from_version`/`to_version` = `0.1.0.dev18` 的来源 | 我只核了 27 条键与精确匹配逻辑，未核版本号从哪个源读取 |
| 证据文档 `docs/validation/2026-11-15-engine-readiness-expiry.md` 的「rebase 前」中间时点数据 | 我只复现了 head `11bf6e1b` 与基线 `origin/main` 两个可复现点 |

## 复现命令

```bash
# 审查 worktree（报告唯一差异）
git worktree add /private/tmp/sn852 -b review/852-readiness-deltas origin/main
# 被审代码（只读上下文 + 变异副本）
git worktree add --detach /private/tmp/sn852-run 11bf6e1bdbd81cf6bb8e9106d70acfe9d0bbe87a
cd /private/tmp/sn852-run && uv sync --frozen --all-packages && pnpm install --frozen-lockfile

# 例数
for d in tests/unit/backend tests/unit tests/contract tests/ci; do
  uv run --no-project python -m pytest "$d" --collect-only -q -p no:cacheprovider | tail -1
done
uv run --no-project python -m pytest tests/unit tests/contract tests/ci -q -p no:cacheprovider

# 契约
make contracts-check && make contracts-drift
CONTRACT_BASE_REF=origin/main make contracts-speech-changes

# 一次性 PG / MySQL（用完 docker rm -f）
docker run -d --name sn852-pg -e POSTGRES_USER=speechnexus -e POSTGRES_PASSWORD=speechnexus_test \
  -e POSTGRES_DB=speechnexus_test -p 127.0.0.1:55462:5432 postgres:16.15-alpine
docker run -d --name sn852-mysql -e MYSQL_ROOT_PASSWORD=speechnexus_test \
  -p 127.0.0.1:55463:3306 mysql:8.4.8
SPEECHNEXUS_TEST_DATABASE_URL='postgresql://speechnexus:speechnexus_test@127.0.0.1:55462/speechnexus_test' \
SPEECHNEXUS_TEST_MYSQL_URL='mysql://root:speechnexus_test@127.0.0.1:55463/' \
  uv run --no-project python -m pytest \
    tests/integration/test_public_job_admission_http_pg.py \
    tests/integration/test_engine_registration_pg.py \
    tests/integration/test_production_admission.py \
    tests/integration/test_voice_drafts_pg.py \
    tests/integration/test_public_jobs_http.py \
    tests/integration/test_public_job_atomic_admission_pg.py -q
```

关键变异（都在 `sn852-run` 内，跑完 `git checkout -- <file>`；**改完记得清 `__pycache__`**，
见 §F13 的过程记录）：

| 编号 | 变异 | 期望 / 实测 |
| --- | --- | --- |
| A1.1 | `public_job_admission._PUBLIC_CODES.pop("readiness_expired")`（词汇表仍含它） | 实测 `500 text/plain Internal Server Error` |
| A1.2 | store 返回 `error_code="engine_retired"` | 实测 `500 application/json admission_state_invalid` |
| A1.3 | voice-draft store 抛 `KeyError` | 实测 `500 application/json voice_draft_contract_violation` |
| A2 | `_ADMISSION_REJECTION_CODES` 改回手写集合 | `1 failed` |
| A2b | 只改 `PUBLIC_ADMISSION_CODES["readiness_expired"]` | `3 failed` |
| A3 | `_STORE_MECHANISM_CODES` 并入 `engine_retired` | **36 passed + `503 job_unavailable`（假绿）** |
| B4a | `jobs_sql.py` 的 `error_code` 换成数据库读出的变量 | **36 passed（假绿）** |
| B5 | 新增第 14 个模块 raise 未登记码 | **36 passed（假绿）** |
| B7a/b/c | 作者三条反例 | `3 failed` / `1 failed` / `assert 12 == 13` |
| 对照 | `owners.py` 动态传码（producer 型） | `4 failed` |
| E12 | `unreplayablePriorAttemptClause("admission_deadline_elapsed")` | 回退 413 文案 |

## 清理

`/private/tmp/sn852-run` 内所有变异已 `git checkout --` 复原（收尾 `git status` 只剩临时探针，
探针随后移出）；探针（`tests/integration/test_zz_probe_a11.py`、
`tests/unit/backend/test_zz_probe_round3.py`、`web/workbench/src/file-workflow/zz-probe-e12.test.ts`）
已删除/移出；`uv sync` 改写的 `uv.lock` 已 `git checkout --` 复原；
`sn852-pg`、`sn852-mysql` 两个一次性容器在审查结束 `docker rm -f`。
未 push main，未合并，未改动任何既有 `docs/reviews/` 文件。
