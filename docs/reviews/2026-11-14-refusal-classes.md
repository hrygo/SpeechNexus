# 渲染派发拒绝族补全的非作者审查（PR #770 / issue #754 + #755）

> 过程记录文档：保留时间线、独立复现的反例与命令输出。结论面向合并决定，不修改产品代码/契约/测试。
> 审查对象：`fix/754-755-refusal-classes`，head `537a49d304c05eb01ef2587fef9fcad772ab47fd`（下称 head），PR 合并基点 `56d2e05e235895a9f5f02857ce4fbf76ee260033`。
> 本报告分支 `review/774-refusal-classes` **基于 `origin/main`** 创建（落点 `e4ba64b7438c3b6afb8be38e38068d1ea2c26b4d`，即审查开始时的 `origin/main`），`git diff origin/main...HEAD --stat` 只含本文件。审查期间 `origin/main` 前进到 `1d368577`（#773「test: 去掉 binding 超时用例重放路径的 100ms 时序假设」），该笔只改 `tests/integration/test_binding_http.py` 一个文件，不触及渲染派发、工作台码表、契约或工作台 Web 代码（`git diff --name-only e4ba64b7 1d368577` 仅此一文件），对下文所有判定与例数影响为零；报告已 rebase 到新 `origin/main`。
> 被审内容一律用 `git show 537a49d3:<path>` 或 detached 快照 worktree `/private/tmp/sn774-pr`（`git worktree add --detach ... 537a49d3`）读取，**没有**从被审 head 拉分支；基线副作用用 `origin/main` 侧 worktree `/private/tmp/sn774` 对照。
> 环境：系统 node **v26.12.1**（`~/.local/share/mise/shims/node`）、pnpm **12.14.0**、uv **0.13.5**、Python **3.14**（`uv sync --locked --all-packages --all-groups`）。两个 worktree 各自装依赖，`uv.lock` / `pnpm-lock.yaml` 未被改动。
> 实际审查发生在 2026-11-14 19:11–19:5x（本地时间，Asia/Shanghai）。
> 报告文件名 `2026-11-14-refusal-classes.md` 在 `docs/reviews/`（42 个既有文件）下**不存在**，未覆盖任何既有审查记录；最接近的既有记录是 `2026-11-14-render-refusal-vocabulary.md`（`#750` 词表审查）与 `2026-11-14-cancel-settled.md`（`#723` 取消结算审查），本报告与之并列。

## 结论

**核心修复正确，守卫真实咬人，例数与五条反例全部独立复现一致；但发现 1 条合并前必须修的契约准确性问题，以及 2 条应当修。建议小修后合并。**

判定依据（全部独立实跑，见后文各节）：

- **`#754` 修对了**：`429` 由 `_REFUSAL_STATUSES` 收进拒绝族（`public_speech_render_jobs.py:57`），不再落进 `RenderSubmissionUnknownError`；面板侧 `429` 也从「结果未知」改为可重试拒绝（`RenderUnitPanel.tsx:225` 起的 `problemOf`，以及 `:492` 的 journal 结算谓词）。
- **`#755` 修对了**：`409` 保留上游码并三分（`http_render_units.py:85` 的 `_CONFLICT_CODES` + `:89` 的 `_CONFLICT_FALLBACK`），`401/403/415` 各自成码（`:97` 的 `_STATUS_REFUSALS`），不再折成 422。
- **五条反例全部按预期红，且失败数逐条对上作者自报的 12 / 9 / 5 / 9 / 3**（见 D10）。
- **`#723` 的取消结算没有被本 PR 断掉**（见「核心判断 B」）：`settlesCancelAttempt` 认的是**上游 speech 码**，而本 PR 只改工作台码表，取消路径涉及的四个文件在 head 与基线**逐字节相同**。
- **`docs/reviews/` 既有语义不回归**：`#750` 的 `param: null` → 兜底、本地契约拒绝走本地探针均保持；`upstream_unreadable` 的口径**有变化但被显式记录**，除 `429` 一例外可接受（见 A3 / D11）。
- **但**：本 PR 把 `409 render_submission_cleanup_pending` 作为「可重试的清理等待」写进了公开契约（`workbench-openapi.yaml:2204`）与面板文案，而**该码在本入口根本不可达**——渲染派发只调 `createJob`，`createJob` 不可能返回 `execution_cleanup_pending`（见「核心判断 A」）。这不是功能缺陷，是**公开契约承诺了服务端永不发送的分支**，属契约准确性问题，合并前必须处理。

## 核心判断 A：`execution_cleanup_pending` 的可重试性是否与上游矛盾

### A-1 上游事实核实（`speech/interfaces/http/job_errors.py`）

上游的可重试标记由**一个通用辅助函数内的固定三元组**计算，不是逐条件语义：

```python
# backend/src/speechnexus_backend/speech/interfaces/http/job_errors.py:142-146
retryable = code in (
    "job_outcome_unknown",
    "job_capacity_exhausted",
    "job_admission_contended",
)
```

`execution_cleanup_pending` 不在该元组内 → 上游对它的线上值确实是 **`retryable: false`**。它只由 `_cancel`（`jobs.py:615`）与 `_retry`（`jobs.py:640`）经 `job_error(...)` 发出：

```
$ grep -rn "execution_cleanup_pending" --include=*.py backend/src/ | grep -v test
backend/src/.../domain/leases.py:167        raise LeaseRejectedError("execution_cleanup_pending")
backend/src/.../domain/lease_release.py:127 / :132 / :155
backend/src/.../interfaces/http/job_errors.py:22          (错误表条目)
backend/src/.../interfaces/http/jobs.py:615               _cancel → job_error("execution_cleanup_pending", ...)
backend/src/.../interfaces/http/jobs.py:640               _retry  → job_error("execution_cleanup_pending", ...)
```

**所以「上游把它标成不可重试」在字符串层面是真的**，作者没有误读这一点。

### A-2 但工作台根本收不到这个码（本报告最重要的独立发现）

渲染派发适配器只有两条上游调用，`:148` 的 `submit` 与 `:190` 的 `read_job`：

```
$ grep -n "self._client\.\|/v1/nexus" backend/src/.../infrastructure/public_speech_render_jobs.py
137:  response = await self._client.post("/v1/nexus/jobs", json=command, headers=headers)   ← createJob
159:  response = await self._client.get(f"/v1/nexus/jobs/{job_id}")                        ← getJob
```

而 `createJob`（`jobs.py:446 _create`）的异常处理表里**没有** `JobCleanupPendingError`；动态分支 `job_error(error.code)` 的唯一来源是 `JobAdmissionRejectedError`，其码集 `_ADMISSION_REJECTION_CODES`（`application/public_jobs.py`）也不含该码。用 AST 枚举可达码：

```
$ PYTHONPATH=/private/tmp/sn774-pr uv run python /private/tmp/probe-reachability.py
_create: literal=['asset_not_ready', 'idempotency_conflict', 'invalid_job_request',
 'job_kind_not_supported', 'job_not_found', 'job_outcome_unknown', 'job_request_too_large',
 'job_unavailable', 'model_not_published', 'profile_not_published', 'pronunciation_not_found',
 'unsupported_parameter', 'voice_draft_not_candidate', 'voice_draft_not_generatable',
 'voice_draft_stale_revision', 'voice_not_published', 'workspace_disabled', 'workspace_not_found']
  dynamic(job_error(error.code))=True
_cancel: literal=['execution_cleanup_pending', 'idempotency_conflict', 'job_not_found',
 'job_outcome_unknown', 'job_unavailable']
_retry:  literal=[... 'execution_cleanup_pending' ...]

admission rejection codes (11): ['asset_not_ready', 'idempotency_conflict', 'job_admission_contended',
 'job_binding_incomplete', 'job_capacity_exhausted', 'job_text_asset_unsupported',
 'model_binding_task_not_supported', 'unsupported_parameter', 'voice_draft_not_candidate',
 'voice_not_published', 'workspace_not_configured']
execution_cleanup_pending in admission: False

createJob reachable code count: 24
execution_cleanup_pending reachable from createJob: False
idempotency_conflict reachable from createJob:     True
job_retry_not_allowed reachable from createJob:    False
job_retry_unavailable reachable from createJob:    False
```

`RenderJobPort` 只有 `PublicSpeechRenderJobs` 一个实现（`grep -rn "RenderJobPort" backend/src/` 只命中 Protocol 定义与构造参数），不存在第二套会调 `cancelJob`/`retryJob` 的适配器。

**结论：`_CONFLICT_CODES["execution_cleanup_pending"]` → `render_submission_cleanup_pending` 在这个入口是死分支。** 随之一起失去生产者的还有：`ERRORS` 里的该码（`http_versions.py`）、`_RETRYABLE_REFUSAL_CODES` 里的该码、契约正文 `workbench-openapi.yaml:2204` 的整段描述、`render-refusals.ts:95` 的面板文案、以及两个 `tests/unit/backend/...` 用例。

### A-3 那么「工作台这样标记是否会误导调用方」

分三层回答：

1. **今天不会误导**：该码不会被发出，因此 `X-Should-Retry: true` 与 `error.retryable: true` 也不会针对它被发出。工作台的 `retryable` 从未被真实发送到这条分支上。
2. **工作台也没有「读上游 `retryable`」**：`_refusal()`（`public_speech_render_jobs.py:99`）只读 `status_code / code / param / Retry-After`，**完全忽略上游信封里的 `retryable` 字段**（测试里 `upstream_error(..., retryable=True)` 传了它也无人消费）。因此这不是「两个字段打架」，而是两个契约各说各话。
3. **语义上工作台的选择站得住**：上游该三元组的注释自称「Only an unknown execution outcome promises that retrying can succeed」，却同时把 `job_capacity_exhausted`（明确不是 unknown）放进去，说明该标记是**为「未知结果能否重放」这一根轴校准的粗粒度允许表**，不是逐条件的可重试语义。对「上次执行未确认停止」而言，「等清理完成再派发」确实是可行的下一步，与 issue `#755` 的验收要求（「不得把清理中当 terminal」）一致。

**所以真正的缺陷不是「可重试性搞反」，而是**：一个**不可达**的分支被当成 `#755` 的核心交付写进了公开契约与面板，而唯一声称能拦住这类问题、名为 `test_the_mapping_only_names_codes_the_upstream_contract_declares`（docstring 原文：「A mapped code that upstream never sends would be an unreachable branch」）的守卫，检查的是**上游整份词表**（`_ERRORS`），不是**本路由的可达码集**——所以它放过了一个真正不可达的分支。这是守卫口径与其自我声明不符（C7）。

**假如未来该路径变成可达**（例如工作台开始调 `retryJob`/`cancellations`），工作台会为同一条件发出与上游相反的 `retryable: true` + `X-Should-Retry: true`。那一刻必须先在上游修 `job_errors.py` 的允许表，或在工作台显式记录「为何故意覆盖上游口径」。现在就应把这条注释钉在 `_RETRYABLE_REFUSAL_CODES` 旁，避免未来静默分叉。

## 核心判断 B：cancel 路径的码名是否被本 PR 断掉

**没有被断掉。** `#723` 的取消结算谓词认的是**上游 speech 码**，而本 PR 只改**工作台码表**，两者不在同一契约面。

证据链（四条独立证据）：

1. **谓词原文未变**，认的是上游码（`web/workbench/src/file-workflow/file-task-session.ts:241-250`）：

   ```ts
   export function settlesCancelAttempt(error: { readonly status: number; readonly code: string }): boolean {
     if (error.status === 404) return error.code === "job_not_found";
     return (
       error.status === 409 &&
       (error.code === "execution_cleanup_pending" || error.code === "idempotency_conflict")
     );
   }
   ```

2. **该文件说话的契约是 speech，不是 workbench**：`file-task-session.ts:1-8` 从 `./speech-file-client` 导入 `SpeechFileClient` / `FileContracts`，错误体类型是 `FileContracts["ErrorResponse"]`；它直连 `/v1/nexus/jobs/{id}/cancellations`，不经过工作台的 `generateRenderUnitV2`。

3. **取消路径涉到的四个文件在 head 与基线逐字节相同**：

   ```
   $ MB=$(git merge-base origin/main HEAD)
   $ for f in web/workbench/src/file-workflow/file-task-session.ts \
                web/workbench/src/file-workflow/speech-file-client.ts \
                backend/src/speechnexus_backend/speech/interfaces/http/job_errors.py \
                contracts/speech-openapi.yaml; do
       git diff --quiet $MB HEAD -- "$f" && echo "UNCHANGED  $f" || echo "CHANGED    $f"; done
   UNCHANGED  web/workbench/src/file-workflow/file-task-session.ts
   UNCHANGED  web/workbench/src/file-workflow/speech-file-client.ts
   UNCHANGED  backend/src/speechnexus_backend/speech/interfaces/http/job_errors.py
   UNCHANGED  contracts/speech-openapi.yaml
   ```

   PR 的 18 个文件里没有 `file-workflow/`、没有 `speech-file-client.ts`、没有 `contracts/speech-openapi.yaml`、没有 `job_errors.py`。

4. **`#723` 的用例在 head 全绿**：

   ```
   $ cd web/workbench && npx vitest run src/file-workflow/file-task-rejection.test.ts
    Test Files  1 passed (1)
         Tests  95 passed (95)
   ```

**为什么容易误判**：本 PR 确实引入了一个**几乎同名**的新码 `render_submission_idempotency_conflict`，而旧的上游 409 在工作台上原本就是 `idempotency_conflict`。但 `settlesCancelAttempt` 在**文件任务**路径上，那条路径拿到的始终是上游码；渲染路径的结算走的是另一套**按状态**的谓词（`RenderUnitPanel.tsx:492` 的 `rejected = status 4xx && != 408`），**不按码名匹配**，因此码名变动不会影响它。

**顺带确认渲染路径 `429 → 已结算` 是安全的**（这是本 PR 在渲染侧的「#723 等价物」改动）：`application/render_units.py:294` 先 `job_id = await self._jobs.submit(...)` 再 `self._units.start_unit(...)`，拒绝发生在写 attempts 行之前——工作台不留「进行中」的假尝试，上游 429 也不写 Job 行，所以面板「用新键重新派发」的下一步真的可执行，且用旧键同样安全。

## 十四条逐条判定

| # | 项 | 判定 |
| --- | --- | --- |
| A1 | `execution_cleanup_pending` 可重试性 vs 上游 | **矛盾真实存在但不可达**：上游确发 `retryable: false`（`job_errors.py:142-146`），工作台标 `true`；但本入口收不到该码（`createJob` 可达 24 码不含它），且工作台从不读上游 `retryable`。语义可辩护，**真实缺陷是不可达分支被写进公开契约 + 守卫口径不符**。需修 |
| A2 | `Retry-After` 只认整数秒 | **可接受，非降级**：上游契约自己声明 `RetryAfter: type: integer, minimum: 0`（`speech-openapi.yaml:2616-2620`），HTTP-date 本就在契约外；丢弃后仍保留「可重试但无延时」的安全降级，面板回落到不带秒数的文案。仅缺 debug 日志（P3） |
| A3 | 任意 429 → `capacity_exhausted` | **对声明面精确，对不可读 429 过度断言**：上游词表里 429 只有 `job_capacity_exhausted` 一个码，故对声明面无误；但 `RenderRejection.unreadable(429)` 也发布「作业容量已用尽」（实测输出见 C8），429 恰恰无法区分容量与限流。面板已有「限流或容量不足」的模糊措辞（`retryLater`），却只用于**未知码**。应修 |
| B4 | `403`/`415` 声明 + `Forbidden` 组件 | **必要修复，非脚手架式扩张**：因果链真实且已实测（见下）。`415`/`UnsupportedMediaType` 组件**基线就存在且被约 18 个操作使用**，本 PR 只是让这一个操作跟上；`403` 在此契约里确为首次，`Forbidden` 组件（`workbench-openapi.yaml:2509`）必须新增 |
| B5 | `error.retry_after_ms` 是否已声明 | **通过，作者陈述为真**：`ErrorBody` 中 `retry_after_ms` 是**既有**可选字段（`workbench-http.schema.json:3916`，与 merge-base 同行号、零 diff），带 `additionalProperties: false` 但该字段已列出；TS 侧 `workbench.generated.ts:1871` 也有 `retry_after_ms?: number \| null`。写入响应体不会让严格消费者校验失败 |
| B6 | `contracts-http-changes` 通过的含义 | **通过≠没问题，本次新增恰在该工具自陈的未覆盖区**：工具输出明写未验证范围含「HTTP serialization settings, **added statuses/media** and security scheme evolution」。本 PR 新增的 `403`/`415` 响应声明正落在该区。需人眼补足的部分见 C7/B4：组件存在性已核、与兄弟操作一致性已核（415 广泛在用）、唯一消费者（`book-journal.ts` 的请求校验 + 面板）已更新 |
| C7 | 7 个新码三侧一致 + 守卫覆盖 | **通过（附守卫口径缺陷）**：`_backend_codes()` 取 `ERRORS` 里全部 `render_submission_*`，面板与契约正文都与它比较，7 个新码全部纳入；`test_every_refusal_code_and_status_maps_to_a_published_disposition` 对 `REFUSAL_STATUSES × 全部上游码 × 3 个 param` 断言产物必须在 `ERRORS`，覆盖了 `_STATUS_REFUSALS`/`_CONFLICT_FALLBACK`。**但**「上游词表」不等于「本路由可达码集」，故放过 A-2 的死分支 |
| C8 | 面板是否真的呈现等待秒数 | **链路完整且实测贯通**：后端 `retry_after_ms`（实测 3000/7000/2000）→ `WorkbenchResponseError.retryAfterMs` → `retryAfterSeconds()`（`RenderUnitPanel.tsx:110`，`Math.ceil(ms/1000)`，≤0 不渲染）→ `refusalStep()`（`render-refusals.ts:116`）追加「服务端要求至少等待 N 秒（Retry-After）」；jsdom 用例断言 `等待 3 秒` / 未知 429 码 `等待 2 秒`。**通过** |
| C9 | 删除的泛指文案是否有残留 | **通过**：`请核对单元文本或声音` 在产品代码中零命中，只在 `RenderUnitPanel.test.tsx:624,862` 作为 `not.toContain` 反向断言、以及 `docs/reviews/` 历史记录中出现（历史记录应保留）。`render_submission_rejected` 的兜底文案仍含「核对单元文本与固定字段」，是**保留的兜底**而非残留，且先陈述事实再给建议 |
| D10 | 五条反例独立复现 | **通过，五条全红且计数逐条对上 12/9/5/9/3**（见下「独立复现」） |
| D11 | `#750` 语义不回归 | **两条保持，一条口径变更**：`param: null` 的 `unsupported_parameter` → `render_submission_rejected` ✅；本地契约拒绝走本地探针 ✅；`upstream_unreadable` 由「一律兜底」变为「400/413/422 兜底，401/403/409/415/429 按状态命名」——**显式记录且可辩护**（状态是观测事实，未冒充上游码），但 `429` 那一格过度断言（同 A3） |
| D12 | `#723` 取消结算是否被破坏 | **未破坏**（见「核心判断 B」） |
| E13 | 范围 / lockfile / 新依赖 | **通过**：18 文件全为 `M`（无新增/删除文件），无 `uv.lock`/`pnpm-lock.yaml`/`pyproject.toml`/`package.json` 改动，无新依赖。除作者自报的三处外，真正的「额外越界」只有 `docs/architecture/speech-api.md` 一段（公共协议改变须同步文档，属**必需**而非越界）与两个生成产物（`source-inventory.json` / `audit json`，属再生成） |
| E14 | 例数与 CI | **通过，六项例数全部精确复现**（见下）；CI 18 绿 + 2 skip；前一次失败原因核实为 `test_operation_coverage.py::test_committed_map_is_current` 的 `contract_drift` |

## 必须修清单

### M1（合并前必须修）契约承诺了本入口永不返回的 `409` 分支

`contracts/workbench-openapi.yaml:2197-2208` 把 `render_submission_cleanup_pending` 写成 `generateRenderUnitV2` 的第三种 409 答案并承诺「wait for the cleanup, then retry. Retryable.」；`render-refusals.ts:95` 也给了对应操作步骤。但该码在本入口不可达（A-2 全部证据）。这属于**公开契约过度承诺**：消费者会为一条永不到来的分支写处理逻辑，评审/测试也会被引向一个不存在的场景。

最小改法（二选一）：

- **(a) 收敛**：从 `_CONFLICT_CODES`、`ERRORS`、`_RETRYABLE_REFUSAL_CODES`、契约正文、`render-refusals.ts`（码表 + 文案）删除 `cleanup_pending` 一支，面板 `RENDER_REFUSAL_RETRYABLE` 只留 `capacity_exhausted`。
- **(b) 保留前瞻**：保留实现，但把契约正文改为「该码为保留码，当前渲染派发不经由 cancel/retry 入口，不会返回」，并把 `_CONFLICT_CODES` 的注释写清可触发条件；同时**补一条可达性守卫**——断言 `_CONFLICT_CODES` 的每个键都出现在 `createJob` 的可达码集（或显式登记为前瞻保留），让 `test_the_mapping_only_names_codes_the_upstream_contract_declares` 的 docstring 名实相符。

推荐 (b)：`#755` 明确要求区分「清理中」，该码在 issue 语境里是有意义的；把不可达这一事实写进契约即可，不必删。

### S1（应修）`429` 的可读性断言超过状态能证明的范围

`RenderRejection.unreadable(429)` 与「429 + 非 `job_capacity_exhausted` 码」都会发布 `render_submission_capacity_exhausted`，面板给出断言式文案「作业容量已用尽」；而 429 在 HTTP 语义上首先意味着「请求太频繁」，前置代理/限流层完全可能返回契约外 429。今日上游词表只有容量一个 429 码，故不是现实缺陷，但**码名与文案断言了一个未被读到的原因**，正是 `#755` 想避免的「错误归因」的另一面。

建议：把「不可读 / 码不在容量白名单」的 429 归到已有的模糊措辞路径（`render-refusals.ts:117` 的 `retryLaterMessage`「限流或容量不足」），或把该码名去具体化（例如 `render_submission_admission_refused`），让断言与证据等长。

### S2（应修）工作台的 `retryable` 与上游口径的分叉没有留痕

见 A-3 第 3、4 点。现在不可达所以无害；一旦该路径可达，工作台会就同一条件发出与上游相反的 `retryable`。建议在 `http_render_units.py:109` 的 `_RETRYABLE_REFUSAL_CODES` 旁注明「上游 `job_errors.py` 的允许表不含 `execution_cleanup_pending`；此处刻意按处置而非上游标记声明，理由：…」，避免未来有人据上游字段「修正」这里。

## 建议（不阻塞）

- **P1（既有缺陷，建议单独开卡）** `generateRenderUnitV2` 未声明 `500`（head 与 merge-base 都没有），因此路由 `except Exception: return _error("internal_error", ...)` 自身会抛 `ContractValidationError`: `_error(internal_error) RAISED: ContractValidationError Invalid HTTP response`，客户端拿到的是裸 `500 Internal Server Error` 而非声明的 JSON 错误体。本 PR 的 B4 论据依赖同一个校验器，且新增了进入该路径的方式；建议补声明 `500`。
- **P2** 生成入口存在两个「键被占用」码：`idempotency_conflict`（工作台层，`http_versions.py:119`；`generateRenderUnitV2` 路径上不可达，因为 `postgres_render_units.start_unit` 只抛 `ResourceNotFoundError`，抛该码的是 `create_unit`）与新码 `render_submission_idempotency_conflict`（上游 409）。二次步相同导致 `RenderUnitPanel.tsx:53` 的 inFlight 文案成为死分支，且两处措辞不一。建议清理或合并表述。
- **P3** `Retry-After` 非整数被**静默**丢弃（`public_speech_render_jobs.py:84-97` 无日志）。契约合规，但建议加一条 debug 日志便于发现上游口径漂移；另 `_RETRY_AFTER` 正则不接受空白与前导零（`" 1"` / `"01"`），比 RFC 更严，现实无影响。
- **P4** 真实浏览器覆盖未增加：`test:behavior` 仍是 4 条且全部是 file-task 场景，渲染拒绝族的用户可见呈现只有 jsdom 覆盖。若认为「面板真的显示了秒数」需要端到端证据，建议补一条 render 场景的 behavior 规格。
- **P5** `render_submission_rejected` 的兜底文案仍以「核对单元文本与固定字段」收尾，与本 PR 批评的泛指措辞同类（虽然它先陈述了事实）。可选统一。

## 独立复现的证据

### 1. 例数（PR head vs 基线）

| 套件 | PR head `537a49d3` | 基线 `origin/main` `e4ba64b7` | 作者自报（基线 `56d2e05e`） |
| --- | --- | --- | --- |
| `uv run pytest tests/contract -q` | **3797 passed** | 3785 passed | 3797（基线 3760） |
| `uv run pytest tests/unit/backend -q` | **3130 passed** | 3092 passed | 3130（基线 3092） |
| `pnpm test`（web/workbench） | **914 passed / 63 files** | — | 914 / 63（基线 902 / 63） |
| `pnpm test:behavior`（根，需先 `pnpm build`） | **4 passed** | — | 4 |
| `pnpm test:a11y`（根） | **52 passed** | — | 52 |
| `pnpm typecheck` / `pnpm build` | 通过 / 通过 | — | 通过 / 通过 |
| `uv run pytest tests/unit/test_operation_coverage.py -q` | 15 passed | — | CI 首次在此失败 |

注：作者基线是 PR 合并基点 `56d2e05e`，我实测基线取**当前** `origin/main`（`e4ba64b7`，已前进），故 contract 基线比作者多 25 例、head 同为 3797 与作者一致（增量为 +12 相对当前 main、+37 相对作者基线）。`tests/unit/backend` 两处基线同为 3092。

`test:behavior` / `test:a11y` 是**仓库根**的 Playwright 脚本（`playwright.behavior.config.mjs` / `playwright.a11y.config.mjs`），且 webServer 需要 `web/admin` 与 `web/workbench` 的 `dist`——必须先 `pnpm build`（根递归），只在 `web/workbench` 内 `pnpm build` 不够（实测 `vite preview` 报 `The directory "dist" does not exist`，`web/admin` 失败）。两者严格串行执行，未并发。

### 2. `A1` 的上游定义与可达性

见「核心判断 A」全部命令输出（`job_errors.py:142-146`、`jobs.py:615/640`、AST 可达码探针）。

### 3. `B4` 的 500 因果链（变异实测）

校验器在状态未声明时直接抛错：

```python
# packages/contracts/src/speechnexus_contracts/http_validation.py:88-90
operation_metadata = _operation(profile, operation)
if type(status) is not int or str(status) not in operation_metadata["responses"]:
    raise ContractValidationError("Invalid HTTP response")
```

变异（单点，1 hunk）：从生成的 `workbench-http.schema.json` 的 `x-nexus-http-operations.generateRenderUnitV2.responses` 删除 `"415"`：

```
$ git diff -U0 HEAD | grep -c '^@@'
1
responses now: ['202', '400', '401', '403', '404', '409', '422', '429', '503']
observed status: 500
observed body: Internal Server Error
```

未变异时同一路径正常返回 415（见 C8 探针第 6 行）。**「不声明 415 就不是 415 答案而是 500」这条因果链成立**，故 B4 是必要修复而非扩张。还原后 `git status --porcelain` 为空、0 hunk。

### 4. `B5` 的 `retry_after_ms` 声明

```
$ grep -n "retry_after_ms" packages/contracts/src/speechnexus_contracts/workbench-http.schema.json
3916:        "retry_after_ms": {          ← 与 merge-base 同行号，零 diff
$ ... External_shared_errors_schema_json_ErrorBody
  "required": ["code","type","message","param","request_id","retryable"],
  "additionalProperties": false,
  properties: ..., "retry_after_ms": {"type": ["integer","null"], "minimum": 0}, "nexus": {...}
$ grep -n "retry_after_ms" packages/contracts/src/workbench.generated.ts
1871:      retry_after_ms?: number | null;      ← 既有，非本 PR 新增（PR 只加了一行文档注释）
```

### 5. `C8` 的呈现链路（真实响应体 + 面板行）

真实后端响应（`_Service(RenderSubmissionRejectedError(...))` 经 ASGI 路由实测）：

```
--- 429 capacity, upstream Retry-After=3s: HTTP 429 {"x-should-retry": "true", "retry-after": "3"}
{"error": {"code": "render_submission_capacity_exhausted", "type": "rate_limit_error",
 "message": "Job admission found no capacity and created no Job; retry after the stated delay.",
 "param": null, "request_id": "...", "retryable": true, "retry_after_ms": 3000}}
--- 429 unreadable envelope, header=7s: HTTP 429 {"x-should-retry": "true", "retry-after": "7"}
{"error": {..., "code": "render_submission_capacity_exhausted", ..., "retryable": true, "retry_after_ms": 7000}}
--- 409 execution_cleanup_pending, header=2s: HTTP 409 {"x-should-retry": "true", "retry-after": "2"}
{"error": {"code": "render_submission_cleanup_pending", ..., "retryable": true, "retry_after_ms": 2000}}
--- 409 idempotency_conflict (terminal): HTTP 409 {"x-should-retry": "false"}
{"error": {"code": "render_submission_idempotency_conflict", ..., "retryable": false}}   ← 无 retry_after_ms
--- 401 unreadable (terminal): HTTP 401 {"x-should-retry": "false"}
{"error": {"code": "render_submission_credentials_rejected", "type": "authentication_error", ..., "retryable": false}}
--- 415 unreadable (terminal): HTTP 415 {"x-should-retry": "false"}
{"error": {"code": "render_submission_media_unsupported", ..., "retryable": false}}
```

客户端到面板：

```ts
// web/workbench/src/manuscripts/workbench-client.ts
readonly retryAfterMs: number | null;
this.retryAfterMs = detail.error.retry_after_ms ?? null;

// web/workbench/src/books/RenderUnitPanel.tsx:110
function retryAfterSeconds(error: WorkbenchResponseError): number | undefined {
  const message = error.retryAfterMs;
  if (message === null || message === undefined || message <= 0) return undefined;
  return Math.ceil(message / 1000);
}
// :237  refusal 分支：只有可重试处置才带延时
return isRetryableRefusal(refusal)
  ? { key: "refusal", code: refusal, retryAfter: retryAfterSeconds(error) }
  : { key: "refusal", code: refusal };

// web/workbench/src/books/render-refusals.ts:116
export function refusalStep(code: RenderRefusalCode, retryAfterSeconds?: number): string {
  const step = RENDER_REFUSAL_STEPS[code];
  if (!isRetryableRefusal(code) || retryAfterSeconds === undefined) return step;
  return `${step} 服务端要求至少等待 ${retryAfterSeconds} 秒（Retry-After）。`;
}
```

面板用例侧断言（head）：`expect(alert.textContent).toContain("等待 3 秒")`（`retry_after_ms: 3000`）、未知 429 码 `retry_after_ms: 1500` → `等待 2 秒`（`Math.ceil`）。

### 6. `D10` 五条反例（每条单点、单独运行、逐次还原）

计分范围：`tests/contract/test_render_refusal_vocabulary.py` + `tests/unit/backend/test_workbench_render_generation_http.py` + `tests/unit/backend/test_public_speech_render_jobs.py`，未变异基线 **124 passed**。

| 变异（各 1 hunk） | 实测 | 作者自报 | 红在预期断言 |
| --- | --- | --- | --- |
| ① `429` 从 `_REFUSAL_STATUSES` 移除（回 unknown） | **12 failed**, 111 passed | 12 | `test_refusals_are_not_reported_as_unknown[429]`、`test_a_429_keeps_the_delay_it_states` 等 |
| ② `capacity_exhausted` 移出 `_RETRYABLE_REFUSAL_CODES`（429 当终态） | **9 failed**, 115 passed | 9 | `test_the_retryable_dispositions_agree_on_all_three_sides`、`test_the_named_retry_directions_are_pinned[429-...-True]` |
| ③ `cleanup_pending` 移出可重试集（清理中当 terminal） | **5 failed**, 119 passed | 5 | `test_the_named_retry_directions_are_pinned[409-execution_cleanup_pending-...-True]` 等 |
| ④ `401/403/415` 从 `_STATUS_REFUSALS` 移除（折回 422） | **9 failed**, 115 passed | 9 | `...[401-invalid_job_request-render_submission_credentials_rejected-False]` 及 403/415 同名 |
| ⑤ 仅后端新增码（面板无处置） | **3 failed**, 121 passed | 3 | `test_the_backend_and_the_panel_agree_on_the_vocabulary`、`test_the_contract_states_the_same_vocabulary`、`test_every_code_has_an_operator_step_and_the_panel_uses_the_list` |

⑤ 的失败触发了三个不同的守卫门，说明「三侧一致」不是装饰。每条变异后均 `git checkout -- <file>` 还原，`git status --porcelain` 为空、`git diff -U0 HEAD | grep -c '^@@'` 归零。④ 是 1 hunk、3 个 dict 条目（同一处语义改动，非复合）；其余四条均为 1 hunk 单点。**未 push 任何变异。**

### 7. `D11` 的 `#750` 不回归表

head（`PYTHONPATH=/private/tmp/sn774-pr`）：

```
A) unreadable envelope -> disposition
   unreadable 400 -> render_submission_rejected            retryable=False
   unreadable 401 -> render_submission_credentials_rejected retryable=False
   unreadable 403 -> render_submission_permission_denied    retryable=False
   unreadable 409 -> render_submission_conflict             retryable=False
   unreadable 413 -> render_submission_rejected             retryable=False
   unreadable 415 -> render_submission_media_unsupported    retryable=False
   unreadable 422 -> render_submission_rejected             retryable=False
   unreadable 429 -> render_submission_capacity_exhausted   retryable=True    ← 过度断言（A3/S1）
B) unsupported_parameter param=None -> render_submission_rejected              ← #750 保持
   unsupported_parameter param='input.text'      -> render_submission_text_too_long
   unsupported_parameter param='voice.revision'  -> render_submission_field_unsupported
C) local_contract param=None -> render_submission_rejected                     ← 本地探针保持
   local_contract param='input.text' -> render_submission_text_too_long
D) None -> render_submission_rejected
```

基线（`origin/main`）：六个拒绝状态下的 unreadable **全部** → `render_submission_rejected`，`_REFUSAL_STATUSES = [400, 401, 403, 413, 415, 422]`（无 409/429）。故「unreadable 不再一律兜底」是本 PR 的**显式口径变更**，head 在 `test_workbench_render_generation_http.py:244-272` 用注释与用例钉住了它（「The status alone is enough, so an unreadable envelope still names them」），属于知情设计而非疏漏。

### 8. 契约门禁（PR head，`CONTRACT_BASE_REF=56d2e05e...`）

```
make contracts-drift              → Contract bindings: up to date            (exit 0)
make contracts-check              → contract validation: passed (37 files)   (exit 0)
make contracts-route-conformance  → OK: every registered speech route matches the contract (exit 0)
make contracts-compat             → OpenAI-compatible contract compatibility: passed (exit 0)
make contracts-http-changes       → Known HTTP structural checks: passed     (exit 0)
make contracts-speech-changes     → Known HTTP structural checks: passed     (exit 0)
```

`contracts-http-changes` 自陈未验证范围（本卡 B6 的判据）：

```
Not a full protocol compatibility proof; see unverified scope:
  - Complete JSON Schema semantic inclusion and standalone JSON contracts
  - HTTP serialization settings, added statuses/media and security scheme evolution
  - WS/event ordering, epoch, authorization and business effects
  - Consumer upgrade, migration and actual deployment qualification
```

### 9. CI 实跑与首次失败原因

```
$ gh pr checks 770          # head 537a49d3
18 pass + 2 skipping（Node Controller image evidence / Build and probe；
                      Docs: markdown code block formatting）

$ gh run list --branch fix/754-755-refusal-classes
2026-11-14T10:55:09Z  537a49d30  completed  success
2026-11-14T10:40:55Z  dafd3e04b  completed  failure
```

失败轮 `43586425005` 的失败 job 与断言：

```
Python: tests, coverage and package build: failure
E  AssertionError: assert [{'code': 'co...est changed'}] == []
E    Left contains one more item: {'code': 'contract_drift', 'id': 'scope', 'detail': 'contract digest changed'}
tests/unit/test_operation_coverage.py:67: AssertionError
FAILED tests/unit/test_operation_coverage.py::test_committed_map_is_current
1 failed, 5774 passed, 5776 deselected ... in 352.34s
```

随后 `537a49d3`（「chore: 刷新工作台操作覆盖审计的契约摘要与审计 head」）把 `docs/audits/2026-10-26-team-c-operation-coverage.json` 的 `contract_sha256` 与 `audited_head` 更新为 `dafd3e04`，重跑全绿。**作者关于「首次因 operation-coverage 刷新失败后重跑」的陈述属实**，且条目级结论未变（仅摘要与 head 两行 diff）。

## 未独立验证 / 只能依赖作者陈述的点

- **集成测试**（`.github` 的 Integration 0–5、PG/MySQL/MinIO、six-shard coverage）：本机未起隔离 PG/MySQL/MinIO，未实跑；只依据 `gh pr checks 770` 的 18 绿。
- **`ruff check` / `ruff format --check .` / `mypy --platform linux` / `lint-imports` / `pnpm lint` / `pnpm format:check`**：未逐条本机重跑，依据 CI 的「Python: lint, types and contract compatibility」（1m50s）与「Web: lint, types, tests, build and accessibility」（2m48s）通过。`uv run --no-project ruff format` 已按要求在报告分支执行（见下）。
- **`Compose` / `Deploy: reference acceptance` / `Node Controller image evidence`** 等非本改动面的门禁：仅读 CI 结论。
- 作者的「五条单点变异各自 1 hunk、已提交树上逐条施加后还原」：我按自己的口径独立复算了五条（计数全部吻合），但**未复用作者的脚本与提交树**，故两者的「同一 hunk」不构成互相证明。
