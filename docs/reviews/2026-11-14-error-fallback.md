# 兜底错误响应降级（PR #783 / issue #777）的非作者审查

> 过程记录文档：保留时间线、独立复现的反例与命令输出。结论面向合并决定，不修改产品代码/契约/测试。
> 审查对象：`fix/777-error-fallback`，head `98061d255ece4ff1c42f12ab7da4c5e2a5092e47`（下称 head），PR 合并基点 `7cd0d8c69609f9d6f4809c7c1828a2d1ce13dc5b`（`git merge-base origin/main 98061d25` 实测，下称基线）。
> 本报告分支 `review/785-error-fallback` **基于 `origin/main`** 创建（落点 `3fb467a645363ae156d0a3e9e9adbd230c759295`，即审查开始时的 `origin/main`，也是 PR 的合并基点），`git diff origin/main...HEAD --stat` 只含本文件。
> 被审内容一律用 `git show 98061d25:<path>` 读取。为了跑通测试与 A/B/C 实测，工作区 `/private/tmp/sn785` 曾在 head 上**detached** 运行，跑完已切回 `review/785-error-fallback`；测试基线用独立 worktree `/private/tmp/sn785-base`（detached `7cd0d8c6`），变异用独立 worktree `/private/tmp/sn785-mut`（detached `98061d25`，每个变异跑完 `git checkout --` 复原，未 push、未落回被审分支）。
> 环境：uv **0.13.5**、Python **3.14.9**、pnpm **12.14.0**、node 由 pnpm 安装到 worktree（`uv sync --all-packages --frozen` + `pnpm install --frozen-lockfile`，`uv.lock` / `pnpm-lock.yaml` 均未改动）。三个 worktree 各自装依赖。
> 实际审查发生在 2026-11-14 21:30–22:0x（本地时间，Asia/Shanghai）。
> 报告文件名 `2026-11-14-error-fallback.md` 在 `docs/reviews/`（45 个既有文件）下**不存在**，未覆盖任何既有审查记录；同目录既有的 `2026-11-14-413-third-state.md`、`2026-11-14-body-bound-413.md`、`2026-11-14-refusal-classes.md` 与本报告并列，未被触碰。

## 结论

**核心方案正确，降级信封合法，状态码零漂移，11 个入口全部改到，变异反例逐条咬人；但「缺口是否会变成永久隐形」这一条只做到了一半——日志 warning 可观测、可测，但没有任何守卫把「当前依赖降级的 `(operation, status)` 集合」钉住，新增缺口在 CI 上是静默的。另有 1 条真实逃逸路径（`prepared.close()` 自身抛异常）和 1 条未知 code 的裸 500 未关闭。**

- **必须修 2 条**：A1 的登记表式守卫（把降级集合钉成可断言的清单）、C6 的 `prepared.close()` 逃逸。
- **应当修 1 条**：C7 的未知 `code` 兜底（本仓库 speech 层同类函数已有先例）。
- 其余判定全部通过，含 B3 三条变异（15 failed / 6 passed，三条一致）与 B4 的 `X-Should-Retry`/`retryable` 同源核实。

判定依据（全部独立实跑，见后文各节）：

- **状态码零漂移**（A2）：`_minimal_error` 与 `_declared_error` 从同一 `ERRORS[code]` 取 `status`，降级不改变状态码；基线/head 逐例对照 10 条里 6 条状态码不变、4 条变化全部是「500 → 契约本就该给的 400/503」的修好，没有一条是「本该 4xx 变成 500」。
- **降级 warning 每次触发且可观测**（A1）：A/B/C 三例各 1 条 WARNING、A2（已声明 500 的对照）0 条；21 条新用例里 2 条 `caplog` 钉住该 warning。
- **降级没跳过结构校验**（B3）：删 `request_id` / 丢 `Cache-Control` / 恢复 fail-open 三条变异均 15 failed / 6 passed。
- **`X-Should-Retry` 与 `retryable` 同源**（B4）：两条路径都只从 `retryable` 参数派生，没有第二条来源。
- **异常文本不进响应体**（B5）：实测响应体只有 `internal_error` 稳定文案；`repr` 只进日志，且 `from None` 抑制过的底层异常文本不会经 `exc_info` 泄漏（下文有 15 行实验）。
- **11 个入口全部改到**（C6）：`grep "except Exception as error"` 命中 11 个 HTTP 入口，逐一 `_unexpected_error`；`streamExportEvents` 无路由、v1 `generateRenderUnit` 无路由。
- **例数对上**（D9）：`tests/unit/backend` 3151（基线 3130，+21）、`tests/contract` 3799（基线 3799）、`contracts-{drift,check,http-changes,route-conformance}` 全绿、CI 18 pass / 0 fail。
- **但**：A1 的「集合登记」缺失是真实缺口；C6 的 `prepared.close()` 逃逸是真实裸 500（已复现）；C7 的未知 code 是同类裸 500（已用构造出的 `LlmError` 复现）。

## 核心判断 A：缺口会不会变成永久隐形

### A1-1 事实：修复前后的故障形态确实从「难看」变成「好看」

基线（`7cd0d8c6`）与 head 的同一组原始报文，我自己跑，不引作者输出：

```
=== BASELINE (7cd0d8c6) ===
A       500  text/plain; charset=utf-8  rid= None     cc= None     | Internal Server Error
A2      500  application/json           rid= f2b16469 cc= no-store | {"error":{"code":"internal_error",...
B       500  text/plain; charset=utf-8  rid= None     cc= None     | Internal Server Error
C       500  text/plain; charset=utf-8  rid= None     cc= None     | Internal Server Error
D       200  application/json           rid= 7b2e0d00 cc= no-store | {"object":"nexus.workbench_status",...
A-dup   400  application/json           rid= 6a951338 cc= no-store | {"error":{"code":"malformed_request",...
B-dup   500  text/plain; charset=utf-8  rid= None     cc= None     | Internal Server Error
C-dup   500  text/plain; charset=utf-8  rid= None     cc= None     | Internal Server Error
B-503   500  text/plain; charset=utf-8  rid= None     cc= None     | Internal Server Error
C-503   500  text/plain; charset=utf-8  rid= None     cc= None     | Internal Server Error

=== HEAD (98061d25) ===
A       500  application/json           rid= a2b8743e cc= no-store | {"error":{"code":"internal_error",...
A2      500  application/json           rid= 7f9f0f9a cc= no-store | {"error":{"code":"internal_error",...
B       500  application/json           rid= 98444646 cc= no-store | {"error":{"code":"internal_error",...
C       500  application/json           rid= 58b43f25 cc= no-store | {"error":{"code":"internal_error",...
D       200  application/json           rid= 63ea9177 cc= no-store | {"object":"nexus.workbench_status",...
A-dup   400  application/json           rid= 750237a0 cc= no-store | {"error":{"code":"malformed_request",...
B-dup   400  application/json           rid= 7133087b cc= no-store | {"error":{"code":"malformed_request",...
C-dup   400  application/json           rid= bf14c4f7 cc= no-store | {"error":{"code":"malformed_request",...
B-503   503  application/json           rid= e3e9eeb4 cc= no-store | {"error":{"code":"store_unavailable",...
C-503   503  application/json           rid= c4055923 cc= no-store | {"error":{"code":"store_unavailable",...

=== status deltas ===
A      base 500 head 500 SAME
A2     base 500 head 500 SAME
B      base 500 head 500 SAME
C      base 500 head 500 SAME
D      base 200 head 200 SAME
A-dup  base 400 head 400 SAME
B-dup  base 500 head 400 CHANGED
C-dup  base 500 head 400 CHANGED
B-503  base 500 head 503 CHANGED
C-503  base 500 head 503 CHANGED
```

比作者自报更全：作者只列了 A/A'/B/C/D 五条，实测基线里 **`B-dup`/`C-dup` 的 400 与 `B-503`/`C-503` 的 503 同样是裸 `text/plain`** —— 也就是说本 PR 修掉的不止「500 没声明」，还有 `IngressError` 的 400 与 `VersionStoreError` 的 503 在这两个操作上未声明导致裸 500（按作者的说法「缺口不止 500」，实测证实）。

修复前是**可见但无用**的故障（裸 500、无 `request_id`、无 envelope，运维和客户端都无从定位）；修复后是**好看且可用的**故障（结构化 envelope、`request_id` 回显、`no-store`、`X-Should-Retry`）。这层收益是实的。代价同样是真的：**在只看 HTTP 响应和 CI 的世界里，9 个操作缺 500 这件事彻底不可见了**。

### A1-2 事实：降级 warning 确实每次触发、且可被测试观测

`_error` 的降级分支（`http_versions.py:290-302`）在 `except ContractValidationError` 里记一条 WARNING，带 `operation`/`code`/`request_id`/原始 `ContractValidationError` 的 `repr`。实测（同一进程，DEBUG 级，`caplog` 等价的日志捕获）：

```
case=A status=500 ctype=application/json
  total_log_lines=15 warning_lines=1
case=A2 status=500 ctype=application/json
  total_log_lines=11 warning_lines=0
case=B status=500 ctype=application/json
  total_log_lines=11 warning_lines=1
case=C status=500 ctype=application/json
  total_log_lines=13 warning_lines=1
```

- A（`generateRenderUnitV2`，无 500 声明）降级 → 1 条 WARNING；A2（`getRenderUnitV2`，**有** 500 声明）不降级 → 0 条 WARNING。**warning 与「真的降级了」一一对应，不是无条件打印。**
- 原始失败本身走 `_unexpected_error` 的 ERROR（含 `exc_info` 堆栈），所以一次请求至少留两条日志：一条 ERROR（哪个异常）、一条 WARNING（哪个声明缺口）。
- 新用例里 `test_the_fallback_records_the_original_failure` 与 `test_the_declaration_gap_is_recorded_with_its_status` 两条 `caplog.set_level(logging.WARNING, ...)` **钉住了 warning 的存在与字段**（`operation=`/`code=`/`request_id=`），所以 warning 不会在某次重构里被静默删掉。

**A1 的结论：warning 是够用的「事后发现」手段，但不够用的「事前拦截」手段。** 具体理由：

1. **warning 只能在请求真的打进来时触发**。一个操作缺 500 声明，只要没有未预期异常命中它，就永远不会打这条 warning；而「缺声明」这件事本身在 CI 上是**零信号**——`contracts-drift` 只比契约与生成物是否一致，`contracts-http-changes` 在 `UNVERIFIED` 里明示「added statuses/media」不覆盖（本 PR 的 `docs/validation/http-contract-change-checks.md` 新段也就是在说这件事），没有任何一条检查会把「`getBookExport` 没声明 500」判红。
2. **缺口的集合会无声增长**。本 PR 只保证「缺口存在时客户端拿到合法信封」，不保证「缺口数量不增长」。新增一个操作、或者给某个操作新增一条 `except` 分支、或者改了某个 `ERRORS` 条目的状态码，都可能新增一条降级路径，而上述任何一条检查都不会红。
3. **warning 是 WARNING 级，不是 ERROR 级**。仓库里 `log_level` 默认 `info`（`backend/src/speechnexus_backend/settings.py:46`），warning 能被采集，但既有运维文档里没有「WARNING 即告警」的约定；把它当门禁需要额外的采集/告警配置，本 PR 没有提供。
4. **测试只钉了两条具体实例**，不是集合。`test_the_declaration_gap_is_recorded_with_its_status` 钉的是 `getBookExport` + `store_unavailable` 一个组合；如果有人新增第 10 个缺 500 的操作，21 条用例全绿。换句话说：**闭合一个缺口（补上声明）不会红，新增一个缺口也不会红。**

### A1-3 判断：需要一个登记表式守卫，且本仓库已有同类先例

**需要。这是必须修项。** 理由不是「warning 不好」，而是「warning 只解决发现，不解决防止增长」——审查清单里这条正是问「会不会变成永久隐形」，而只有把集合钉死才能回答「不会」。

本仓库已经有三处同构先例，说明这不是我额外发明的流程，而是既有惯例：

- `scripts/dev-env/check-http-contract-changes.py:29` 的 `_APPROVED_OPERATION_REMOVALS` 字面量 allowlist + `tests/contract/test_approved_http_operation_removals.py` 的**冻结断言**（注释原话：「New entries must add matching negative cases and update the frozen assertion」）。
- `contracts/AGENTS.md` 的契约目录规则要求变更留证据。
- `tests/unit/backend/test_workbench_http_contract_migration.py` 这类「把当前状态整体钉住」的用例。

建议的最小实现（不要求本 PR 一次到位，但应当是合并前可落地的形状）：

1. 在 `tests/unit/backend/` 新增一条**冻结清单**用例，枚举「当前会被降级」的 `(operation, status)` 集合。构造方式有两条，任选：
   - **静态**：读 `workbench-http.schema.json` 的 `x-nexus-http-operations`，按每个路由的 `except` 分支能产生的 `(code → status)` 与操作声明的 `responses` 求差，把差集字面量写在测试里。优点：不碰网络/服务；缺点：需要把「每个路由的 except 分支」也钉住（否则路由新增分支时清单不更新）。
   - **行为**（更推荐，与现有 21 条同风格）：用 `_FailingService` 的模式对每个操作 × 每种已知异常注入一次，断言「降级发生的 `(operation, status)` 集合 == 冻结清单」。这条用例天然覆盖「新增分支」和「新增操作」两种情况。
2. 清单旁边写一行：**新增条目必须同时给出 issue，并附「为什么这个缺口允许先降级」**；关闭缺口时更新清单并删除对应条目。这样 `git diff` 本身就是登记表。
3. 可选但便宜：把 warning 的消息前缀（`workbench error response outside the operation's declarations`）也钉进清单用例的同一条断言里，保证「可观测性」与「集合」同生同死。

如果作者选择**不**加守卫，那么至少应当在 PR 正文或 `docs/validation/http-contract-change-checks.md` 里明确：「本 PR 之后，9 个操作缺 500 的事实仅由运行时 warning 可见，CI 不覆盖；补声明的后续 issue 是 #XXXX」——把「隐形的期限」写下来。但这等于承认缺口会隐形，我不推荐。

### A2：降级会不会改变状态码 / 会不会掩盖真正的产品 bug

**不会改变状态码，也不会把「本该 4xx」变成 500。**

- **状态码来源**：`_declared_error` 是 `status, kind, message = ERRORS[code]`（`http_versions.py:313`），`_minimal_error` 是 `status, kind, message = ERRORS[code]`（`:357`）。同一个 `code`、同一张表、同一个 `status`。降级路径没有任何一处写死 `500`，也不从异常对象推导状态码。实测 A/B/C 三例降级后的状态分别是 500/500/500，与 `ERRORS["internal_error"] = (500, ...)` 一致；B-503/C-503 降级后是 503，与 `ERRORS["store_unavailable"] = (503, ...)` 一致。
- **「本该 4xx 却 500」的唯一可能**是 `except Exception` 捕到了本该由具体分支处理的异常。逐一核对 11 个入口的 `except` 顺序：`IngressError`（400/408/413/415）、`VersionStoreError`（503）、各领域错误（404/409/422 等）**都排在 `except Exception` 之前**，且 `IngressError` 是 `read_envelope` 唯一的请求校验出口（`validate_request` 失败 → `IngressError("invalid_request")`，见 `http_ingress.py:152`）。所以请求校验失败不会落进 500 分支。实测 `B-dup`/`C-dup` 在 head 上是 400 而不是 500，`B-503`/`C-503` 在 head 上是 503 而不是 500，正是这个顺序在起作用。
- **唯一「500 保持 500」的场景**是「操作本来就缺 500，且真的是未预期异常」——此时 500 是**正确语义**（真的没有更具体的分类），修复前后一致。差异只在响应体：裸文本 vs 结构化信封。
- **反向的语义风险**：本 PR 把「声明缺失」从「穿透成裸 500」改成「结构化 500」，确实让「声明缺失」这件事的**后果**变轻了（客户端不再受罚），但**语义没有变**——服务端仍然是「500 / internal_error」，调用方读到的仍然是「服务端内部错误」。它没有把 500 谎报成 200/4xx。

## 核心判断 B / C：降级响应的合法性与覆盖

### B3 `_minimal_error` 仍经 `parse("ErrorResponse")`——确认没跳过结构校验，变异必红

代码事实：`_minimal_error`（`http_versions.py:344-380`）的 body 由 `parse("ErrorResponse", {...})` 构造（`:357`），**不是**手工 `dict`。这与 `_declared_error` 的 `parse` 是同一道门；降级只跳过 `validate_response` 的**声明门**，不跳过**结构门**。

三条变异（独立 worktree `/private/tmp/sn785-mut`，每条跑完 `git checkout --` 复原，未 push）：

```
M1 恢复 fail-open（except ContractValidationError: raise）：
    15 failed, 6 passed in 0.92s

M2 删 _minimal_error body 里的 "request_id": rid：
    15 failed, 6 passed in 0.33s

M3 删 _minimal_error headers 里的 "Cache-Control": "no-store"：
    15 failed, 6 passed in 0.42s
```

三条变异失败数一致（15/6）不是巧合：21 条用例里 6 条是「已声明状态码的对照用例」（`getRenderUnitV2` 的 500/400/503 三条对照 + 两条日志用例 + …），它们不经过降级，所以三种变异都杀不掉；其余 15 条全部经过降级，任何一种破坏降级信封的行为都会让它们红。

- **M1（fail-open）**证明：如果降级被改回「让异常逃逸」，21 条里 15 条立刻红——这正是 issue #777 的原始缺陷形态。
- **M2（删 request_id）**证明：`parse("ErrorResponse")` 的 `required` 确实包含 `request_id`（`workbench-http.schema.json` 的 `External_shared_errors_schema_json_ErrorBody.required`），**没有跳过校验**。如果 `_minimal_error` 用手写 dict 返回，这条变异会绿——它红，就说明结构校验在咬。
- **M3（丢 Cache-Control）**证明：`Cache-Control` 虽然不在 `ErrorResponse` 的 `required` 里，但被 `validate_response` 的 per-operation header 校验（以及用例里的显式断言）接住。注意 M3 红的原因**不是**结构校验（结构校验只管 body），而是：① 用例显式 `assert response.headers["cache-control"] == "no-store"`；② 对已声明状态码的操作，`validate_response` 会检查 header。这说明「丢头」有独立守卫，不依赖 `parse`。

### B4 `X-Should-Retry` 与 `retryable` 是否同源；`retry_after_ms` 「不臆造」对不对

**同源。** 两条路径都只有**一个**输入：`_error(..., retryable: bool)` 的形参。

- `_declared_error`：body 写 `"retryable": retryable`（`:323`），header 写 `"X-Should-Retry": str(retryable).lower()`（`:333`）。
- `_minimal_error`：body 写 `"retryable": retryable`（`:367`），header 写 `"X-Should-Retry": str(retryable).lower()`（`:377`）。

没有第二条来源（实测 `grep -n "retryable="` 在 `interfaces/` 下只命中 `http_versions.py` 的两处形参传递与 `http_render_units.py:385` 的唯一计算点 `retryable=refusal_retryable(code)`）。所以降级不会让这两个字段分叉。

`retry_after_ms` 「不臆造」的判定：**对，而且这个选择的实际风险比看起来小**。理由是「可重试」与「有延迟」在当前的码表里是同源的：

- `retry_after_seconds` 只在 `retry_after_seconds(rejection, code)` 返回非 `None` 时传入（`http_render_units.py:212-216`），而该函数在 `refusal_retryable(code)` 为 `False` 时直接返回 `None`。
- `_RETRYABLE_REFUSAL_CODES` 只有两个码：`render_submission_rate_limited`（429）、`render_submission_cleanup_pending`（409）（`http_render_units.py:144-149`）。
- 这两个码只由 `RenderSubmissionRejectedError` 分支产生，而该分支属于 `generateRenderUnit` / `generateRenderUnitV2` 两个操作；**这两个操作都声明了 429 与 409**（实测 `['202','400','401','403','404','409','415','422','429','503']` 与 `['202','400','401','404','409','422','429']`）。

因此：**「`retryable=True` 且带 `retry_after_seconds`」的响应在现有码表下不会走到降级路径**（它落在已声明的 429/409 上，`validate_response` 通过）。反过来，走到降级路径的响应 `retryable` 恒为 `False`（`internal_error`/`store_unavailable`/`malformed_request` 等都不在 `_RETRYABLE_REFUSAL_CODES` 里），所以 `retry_after_ms` 的丢失在**当前**是不可达的。这不是「判定对」，是「判定在当前码表下无害」；将来若新增一个「可重试但状态码未声明」的码，这个推论就失效，届时 `_minimal_error` 丢掉延迟会让客户端在 `retryable=true` 时没有 `Retry-After` 可读。

判定：**B4 通过，但建议在 `_minimal_error` 的 docstring 或测试里加一句「当前所有可重试拒绝的状态码都已声明，故降级路径上的 `retry_after_ms` 丢失不可达；新增可重试码时必须同时声明其状态码」**——否则这条不变量会随码表增长而失效。这是建议，不是必须修。

### B5 `_unexpected_error` 是否泄漏内部信息

**响应体不泄漏。** `_unexpected_error`（`:382-392`）记 `_log.error("unhandled workbench failure operation=%s request_id=%s error=%r", ..., error, exc_info=error)`，然后 `return _error("internal_error", rid, operation)`；`internal_error` 的 `message` 是静态文案 `"Workbench request could not be completed."`，`param` 为 `None`。实测 A/B/C 的响应体：

```
{"error": {"code": "internal_error", "type": "api_error", "message": "Workbench request could not be completed.", "param": null, "request_id": "...", "retryable": false}}
```

异常文本（用例里的 `private-synthetic-777`）不出现在 `response.text` 里，用例 `assert PRIVATE not in response.text` 也钉住了。

**日志侧：`repr` 是设计意图，`exc_info` 不放大泄漏。** 关键实验（抑制过的底层异常会不会经 `exc_info` 泄漏）：

```
$ python -c '... raise RuntimeError("sanitized message") from None ... _log.error(..., exc_info=e)'
unhandled error=RuntimeError('sanitized message')
Traceback (most recent call last):
  File "<stdin>", line 10, in <module>
RuntimeError: sanitized message
```

`from None` 抑制的 `__context__`/`__cause__` **不会**出现在 traceback 里。这一点很重要，因为本仓库的适配器正是用 `from None` 清洗底层文本的：

- `minio_object_zone.py:132-135`：`except Exception: raise ObjectZoneUnavailableError from None`，注释原话「Never echo driver text: it can contain the endpoint, bucket or object name」。
- `postgres_versions.py:314`：`raise VersionStoreError(sqlstate=state, commit_unknown=unknown) from None`，而 `VersionStoreError`（`version_ports.py:26-58`）的 message 是静态的（`"workbench store transaction failed"` / `"workbench store commit uncertain"`），`sqlstate` 只是属性、不进 `repr`。

所以 `_unexpected_error` 记到的 `repr` 是**清洗后**的异常，不会把 DSN、bucket、object name 带进日志。**残余风险**（应记录、不必改）：traceback 里有源码绝对路径（如 `/private/tmp/sn785/backend/...`），这是 Python 日志的默认行为，与仓库既有风格一致；如果部署要求日志不含宿主路径，需要全局配置（不在本 PR 范围）。另外，若某个适配器**没有**用 `from None` 清洗就抛出携带敏感文本的异常，`_unexpected_error` 会如实记录——这是「记录真相」的合理取舍，不是本 PR 引入的问题。

### C6 11 个 `except Exception` 是否全部改到；有没有别的入口

**11 个全部改到。** `git grep "except Exception as error" 98061d25 -- backend/src/.../workbench/interfaces/` 的完整清单：

| # | 文件 | 行 | 是否 `_unexpected_error` |
|---|---|---|---|
| 1 | `http_assistant.py` | 137 | ✅ `:139` |
| 2 | `http_books.py` | 170 | ✅ `:172` |
| 3 | `http_downloads.py` | 122 | ✅ `:125`（另有 `prepared.close()` 前置，见下） |
| 4 | `http_exports.py` | 115 | ✅ `:117` |
| 5 | `http_management.py` | 235 | ✅ `:236` |
| 6 | `http_meetings.py` | 149 | ✅ `:150` |
| 7 | `http_render_units.py` | 400 | ✅ `:402` |
| 8 | `http_saved_assets.py` | 106 | ✅ `:108` |
| 9 | `http_teleprompter.py` | 102 | ✅ `:103` |
| 10 | `http_versions.py` | 491 | ✅ `:493` |
| 11 | `http_voices.py` | 100 | ✅ `:102` |

**别的入口核对：**

- `streamExportEvents`：契约里有（`x-nexus-http-operations` 中 `['200','401','404','410']`），但 `backend/src` 下**无任何路由注册**（`git grep streamExportEvents -- backend/src` 为空，仅契约 JSON 命中）。它没有 handler，自然不需要兜底。
- v1 `generateRenderUnit`：契约里有，但 `OPERATIONS`（`http_render_units.py:46-51`）只含 `generateRenderUnitV2`，实测 `POST /v1/workbench/render-units/unit_1/generations` 返回 404，与作者「v1 无路由」一致。
- `http_downloads` 的 `_DownloadResponse` 是 `StreamingResponse`（二进制下载），它**没有** SSE/事件流入口；`iter_events` 只在契约包里有函数，workbench 侧无使用者。
- `http_versions.py:553` 的 `@app.exception_handler(HTTPException)` 不走 `_error` 的声明门（`operation=None`），它把 404/405 的 `status_code` 显式覆写，属既有路径，本 PR 未改。

**`prepared.close()` 逃逸：算必须修。** 作者自报的这条我确认了，而且用真实路径复现了（不是构造假象）：

- 代码事实：`http_downloads.py:122-125` 的 `except Exception as error:` 分支先 `if prepared is not None: await prepared.close()`，再 `return _unexpected_error(...)`。`PreparedDownload.close()`（`downloads.py:95-101`）是 `try: await self.body.close() finally: self.closed = True; self.release()`——**`body.close()` 抛异常时会先传播，`release()` 在 `finally` 里执行**，所以异常会穿出 `close()`。
- 实测复现：让 `prepare()` 返回一个 `status=418` 的 `PreparedDownload`（触发 `validate_response` 失败 → 进入 `except Exception`），并让 `body.close()` 抛 `OSError("spool close failed")`：

```
ESCAPED: OSError spool close failed
...
File ".../http_downloads.py", line 124, in handle
    await prepared.close()
File ".../downloads.py", line 99, in close
    await self.body.close()
OSError: spool close failed
```

客户端拿到 `500 text/plain`、无 `request_id`——**与 issue #777 的原始缺陷完全同形**。概率低（`_VerifiedFile.close()` 是 `SpooledTemporaryFile.close()`，正常路径几乎不抛），但**失败模式与修复目标一致**，且修法极便宜：把 `await prepared.close()` 包进 `try/except BaseException: _log.error(...)` 或用 `contextlib.suppress`，然后**无条件** `return _unexpected_error(error, rid, operation)`。审查清单要求「核实这算不算必须修」——**算**，理由：本 PR 的价值主张是「错误层唯一入口对客户端永远给合法信封」，这条路径是唯一已知的例外，不修等于价值主张留了个已知破口。

### C7 `_error` 对未知 `code` 仍 `KeyError`——前提核实

**前提基本成立，但有一处非静态点，建议加兜底。**

静态核对：从 `interfaces/` 下所有 `_error(` 调用点提取字面量 code，与 `ERRORS` 的 62 个键比对，**全部命中**；动态来源逐个核对：

| 来源 | 类型 | 与 `ERRORS` 的关系 |
|---|---|---|
| `IngressError.code` | `read_envelope` 内全部字面量（`malformed_request`/`request_too_large`/`unsupported_media_type`/`request_timeout`/`invalid_request`） | 全部在表内 |
| `TranscriptSourceError`（= `SpeechSourceError`） | `SourceErrorCode` = `Literal[...]`（`speech_sources.py:8`） | 5 个码都在表内 |
| `DownloadError.code` | **`str`，无 Literal**（`downloads.py:14`） | 构造点全部是字面量（`download_unavailable`/`invalid_request`/`range_not_satisfiable`/`download_too_large`/`download_expired`/`download_not_ready`），都在表内 |
| `LlmError.code`（assistant） | `LlmErrorCode` = `Literal[...]`（`llm.py:6-14`），且 `http_assistant.py:120` 用 `_LLM_CODES.get(error.code, "llm_unavailable")` 兜底 | 8 个码都在表内 |
| `LlmError.code`（meetings） | **`"llm_" + error.code`，无 `.get()` 兜底**（`http_meetings.py:142`） | 8 个码拼出来都在表内，但**没有防御** |
| render 拒绝码 | `_REFUSAL_CODES`/`_STATUS_REFUSALS`/`_CONFLICT_CODES` 的全集，`Literal` 于 `ERRORS` | 在表内 |

所以「现有调用点 code 全来自静态表」在**当前代码**下成立。但 `http_meetings.py:142` 的 `"llm_" + error.code` 是唯一一个**运行时字符串拼接、没有 `.get()` 兜底**的点；`LlmError.__init__` 只标注 `Literal`，运行时不做校验。一旦有人从配置、数据库或上游传入一个不在 `Literal` 里的码，`_error` 会在 `ERRORS[code]` 抛 `KeyError`——而 `KeyError` 不逃出 handler（它是在 except 分支里抛的），会直接变成裸 500。**实测**（构造 `LlmError("bogus_runtime_code")` 注入 `createMeetingMinutes`）：

```
'bogus_runtime_code' -> status 500 ctype text/plain; charset=utf-8 | Internal Server Error
'disabled'           -> status 503 ctype application/json | {"error":{"code":"llm_disabled",...
```

这同样是**与修复目标同形的裸 500**。**建议修**：给 `_error` 加一行未知码兜底（`code = code if code in ERRORS else "internal_error"`，或把 `http_meetings` 改成 `_LLM_CODES.get`），与 speech 层 `binding_error` 的既有做法对齐（`binding_errors.py:71` 就是 `if code not in ERRORS: code = ...` 的兜底）。**这不是必须修**（现有码表下不可达），但它把「错误层唯一入口」这句承诺补全，成本一行。

## D 反例与例数

### D8 三条反例（完整输出）

见上文 B3 的三段。补充说明「稳定通过的 6 条」是哪些：

- `test_an_unexpected_failure_is_a_structured_500[getRenderUnitV2-...]`（已声明 500 的对照，走非降级路径）
- `test_a_malformed_request_is_a_structured_400[getRenderUnitV2-...]`（已声明 400 的对照）
- `test_a_store_failure_is_a_structured_503[..., generateRenderUnitV2]` ×2（`store_unavailable` / `outcome_unknown`，已声明 503 的对照）
- `test_the_fallback_records_the_original_failure` / `test_the_declaration_gap_is_recorded_with_its_status`（日志用例，在前述变异下仍能读日志，故保持绿）

这三条对照用例的设计是对的：它们保证「走了声明门」的路径不会因为降级逻辑的改动而回归。

### D9 例数与检查

| 检查 | head | 基线 `7cd0d8c6` | 结论 |
|---|---|---|---|
| `uv run pytest tests/unit/backend -q` | **3151 passed** | **3130 passed** | +21，与作者自报一致 |
| `uv run pytest tests/contract -q` | **3799 passed** | **3799 passed** | 一致（本 PR 不改契约） |
| `make contracts-drift` | `Contract bindings: up to date` | — | 通过（需先 `pnpm install`，见下注） |
| `make contracts-check` | `contract validation: passed (37 files)` | — | 通过 |
| `make contracts-http-changes`（`CONTRACT_BASE_REF=origin/main`） | `HTTP structural baseline: 3fb467a6...` / `Known HTTP structural checks: passed`，`UNVERIFIED` 明示含 "added statuses/media" | — | 通过；该检查的盲区确认存在，但本 PR 不改契约 |
| `make contracts-route-conformance` | `OK: every registered speech route matches the contract; known gaps are explicit and no new deviation was found.` | — | 通过 |
| `gh pr checks 783` | **18 pass / 0 fail / 2 skipping**（skipping 为 Node Controller image 与 Docs markdown 的条件项） | — | 全绿 |

注：`contracts-drift` 在**裸 worktree** 里会因缺 `node_modules`（`normalize-typescript-headers.mjs` 需要 `openapi-typescript`）失败；`pnpm install --frozen-lockfile` 后通过。这是环境前置，不是 PR 缺陷。另：`tests/contract` 首次跑出现过 2 条 `typescript` 消费者用例红（`pnpm` 依赖刚装、node 进程未稳定），重跑即 3799 全绿，与 head 代码无关。

## 必须修清单

1. **A1：把「当前依赖降级的 `(operation, status)` 集合」钉成冻结断言（登记表式守卫）。**
   本 PR 之后，9 个操作缺 500 的事实只剩运行时 warning 可见；CI 零信号，缺口集合会无声增长。建议在 `tests/unit/backend/` 加一条用例，用与现有 21 条同款的 `_FailingService` 注入方式枚举「每个操作 × 每种已知异常」，断言「实际降级的 `(operation, status)` 集合 == 字面量清单」；清单新增条目必须附 issue 与「为何允许先降级」，关闭缺口时同步删条目。可参考 `tests/contract/test_approved_http_operation_removals.py` 的冻结断言形状。

2. **C6：`http_downloads` 的 `prepared.close()` 自身抛异常仍会逃逸成裸 500。**
   已实测复现（`OSError: spool close failed` → `500 text/plain`，`Traceback` 明确指向 `http_downloads.py:124` → `downloads.py:99`）。修法：`await prepared.close()` 包 `try/except BaseException` 并记 ERROR，然后**无条件** `return _unexpected_error(error, rid, operation)`。这是本 PR「错误层对客户端永远给合法信封」价值主张的唯一已知例外，且与 issue #777 的缺陷同形。

## 应当修（不阻塞合并，但建议同轮或紧接一轮）

3. **C7：`_error` 未知 `code` 的 `KeyError` 兜底。**
   现有码表下不可达，但 `http_meetings.py:142` 的 `"llm_" + error.code` 是唯一无防御的运行时拼接；构造一个越界 `LlmError` 已实测得到裸 500。一行兜底即可（`code = code if code in ERRORS else "internal_error"`，或 meetings 侧改 `.get()`），与 speech 层 `binding_error` 的既有做法对齐。

4. **B4 的补记：在 `_minimal_error` docstring 或清单用例里写明「当前所有可重试拒绝的状态码都已声明，故降级路径上 `retry_after_ms` 丢失不可达；新增可重试码必须同时声明其状态码」。** 现在这个不变量是对的，但只对「当前码表」成立，没有写在任何地方。

## 只能依赖作者陈述的点

以下内容我**没有**独立验证，判定时按作者陈述采信，但标注出来以便后续核对：

- **CI 的实际执行内容**：我只核了 `gh pr checks 783` 的 pass/fail（18 pass / 0 fail），没有逐 job 打开日志核对「Python: lint, types and contract compatibility」「Integration 0–5」等 job 内部实际跑了什么、覆盖了什么。18 pass 是事实，「全绿即证明本 PR 无回归」是我的推断，不是实测。
- **`make contracts-compat` / `contracts-speech-changes` / `contracts-route-conformance` 之外其它 `make contracts-*` 目标**：作者自报全过，我只实跑了 `drift`/`check`/`http-changes`/`route-conformance` 四个。
- **`ruff check/format`、`mypy --platform linux`、`lint-imports`**：我对新增测试文件与 `interfaces/` 目录单独跑了 `ruff format --check` / `ruff check`（均通过），但没有在 head 上跑完整的 `mypy` 与 `lint-imports`；作者自报通过。
- **生产装配下 9 个操作的真实可达性**：我用 `_FailingService` 注入异常来触发降级，没有用真实 PG/MinIO/上游服务端到端跑一遍。降级路径本身与注入方式无关（它只看 `validate_response` 是否拒绝），但「9 个操作在生产中实际会被哪些异常命中」是作者基于代码的推断，我未逐一验证。
- **`docs/validation/http-contract-change-checks.md` 新段的措辞是否与 `contracts/AGENTS.md` 的契约目录规则完全一致**：我读了新段与 `contracts/AGENTS.md`，没有发现冲突，但没有做逐条交叉核对。

## 附：命令与 worktree 记录（时间线）

- `2026-11-14 21:30` `git fetch && git worktree add /private/tmp/sn785 -b review/785-error-fallback origin/main` → 落在 `3fb467a6`。
- `21:30` 确认 `docs/reviews/2026-11-14-error-fallback.md` 不存在（`docs/reviews/` 45 个既有文件）。
- `21:31–21:52` 三个 worktree 装依赖：`/private/tmp/sn785`（head，detached 跑测试后切回）、`/private/tmp/sn785-base`（`7cd0d8c6`）、`/private/tmp/sn785-mut`（`98061d25`，三条变异，跑完 `git checkout --` 复原）。
- `21:33` head：`pytest tests/unit/backend/test_workbench_http_error_fallback.py -q` → `21 passed`。
- `21:33–21:36` head：`pytest tests/unit/backend -q` → `3151 passed`；基线 → `3130 passed`。
- `21:37–21:40` head：`pytest tests/contract -q` → `3799 passed`；基线（装 pnpm 依赖后）→ `3799 passed`。
- `21:41–21:48` A/B/C 原始报文对照（基线 vs head，`raise_app_exceptions=False`）、降级 warning 观测、三条变异、C7 构造 `LlmError` 复现、C6 `prepared.close()` 逃逸复现。
- `21:49` `make contracts-drift` / `contracts-check` / `contracts-http-changes` / `contracts-route-conformance` 全绿；`gh pr checks 783` → 18 pass / 0 fail。
- `21:52` 切回 `review/785-error-fallback`（`3fb467a6`），`git diff origin/main...HEAD --stat` 只含本文件。
