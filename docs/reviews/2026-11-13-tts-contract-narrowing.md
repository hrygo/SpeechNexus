---
title: "TTS 正文上限契约收窄与「请求上界收窄」批准登记类（PR #701 / issue #690）的非作者审查"
status: active
version: "0.1.0"
date: 2026-11-14
---

# TTS 正文上限契约收窄与批准登记类的非作者审查（PR #701 / #690）

- **审查对象**：`57df4e902395e95240357875f157d2565a8d1d9f`（`test: 对齐 #700 的准入 HTTP 用例到契约声明的 422 (#690)`），PR #701（分支 `fix/697-tts-contract-limit`，base `main`），对应 issue #690。`git diff --stat origin/main...HEAD`：14 文件、+956/−26。
- **基线**：审查开始时 `origin/main` = `7147f306`（#700）。被审 head 尚未合入 main。
- **工作区**：独立 worktree `/private/tmp/sn710`（`git worktree add /private/tmp/sn710 -b review/710-tts-contract 57df4e90`）。主检出停在 `a7508635 docs/tracker-batch12` 且 `uv.lock` 有未提交改动，本报告未使用其工作树；产品事实一律取自审查 worktree 或 `git show <rev>:<path>`。
- **审查者**：非作者。交付分支只新增本报告，产品代码、契约与测试零改动。
- **变异环境**：两个一次性 worktree —— `/private/tmp/sn710-mut`（同 commit，变异契约；实验后还原）与 `/private/tmp/sn710-main`（`origin/main` 基线，仅用于 web 例数对照）。未 push 任何变异。
- **审查方式**：契约/生成物/消费者静态审查 + 本机实跑七类检查 + 1 组契约回滚变异 + 14 组登记类攻击探针（其中 3 组用真 CLI 复核）+ CI 结论独立核对。
- **报告文件名**：沿用任务指定的 `2026-11-13`；实际执行在 2026-11-14 早间（+08:00）。

> **事实分层**：标「实跑」的都有本机命令输出；只能依赖作者陈述或未实测的项，在末节单列。

---

## 结论

**契约收窄本身成立，可以合并。新增的「请求上界收窄」批准登记类方向正确、绝大多数负例有牙，但它对 finding 的过滤粒度与自证粒度不一致，构成一条真实的越权通道：一条登记项可以把整个 operation 请求体（29 个可达 schema）的任意一处同类改动一起放行，包括收窄、放宽和新增属性。这是本卡的核心问题，建议合并前修。**

- A 部分（收窄是否正确/完整、生成物是否由命令产出、一致性守卫是否真有牙、消费者是否同步）：**四条全部成立**，实跑证据齐全。
- B 部分（登记类是否可被滥用）：**7 组「应该拒绝」的攻击全部按预期拒绝**，但**3 组「换一个位置」的攻击全部静默通过**（详见「独立复现的反例」F1）。
- C 部分（范围与记录）：同步 #700 用例的改动正确，未顺手放宽其它断言；两次中途红的记录基本诚实，但 PR 正文「验证」块停留在中间 commit，与最终 head 不一致；另有一处行为改变被披露却未被测试钉住。

---

## 时间线

| 时间（+08:00） | 动作与结果 |
| --- | --- |
| 06:06 | `git fetch`；确认 `57df4e90` 存在、`origin/main = 7147f306`；建 worktree `/private/tmp/sn710`；读 `contracts/AGENTS.md` 与 `speechnexus-contract-change` 技能规则 |
| 06:08 | 读 14 文件 diff；定位检查内核是 `scripts/dev-env/check-openai-contract-compat.py`（`_KERNEL`）；`uv sync --locked --all-packages --all-groups` |
| 06:10 | 首次全量 `tests/contract`：**44 collection errors**（`No module named 'speechnexus_contracts'`、缺 node_modules）→ 判定为我的环境准备不足，非 PR 问题；补齐 workspace 包与 `pnpm install` 后消失 |
| 06:11 | 一致性守卫 `6 passed`；登记类 `15 passed` |
| 06:12 | 建 `/private/tmp/sn710-mut`；把 YAML 的 `maxLength` 回滚为 200000：守卫 **2 failed, 4 passed**（复现作者「不重生成时 2 failed」） |
| 06:13 | 攻击探针第一轮（改数字、放宽方向、缺 proof、指向别的 operation）：全部 REJECT |
| 06:14 | `web/workbench` 实跑：PR 773 / main 770 |
| 06:15 | 攻击探针第二轮（同级变体收窄 / 同级变体新增属性 / 同级变体放宽）：**三组全部静默通过** → F1 |
| 06:16 | 用真 CLI 在 `/private/tmp/sn710-mut` 复核 F1：契约里多一处未登记的收窄，`check-http-contract-changes.py --surface speech` 仍输出 `passed`、exit 0 |
| 06:17 | 全量 `tests/contract`：**3718 passed**；`ruff check`、`ruff format --check`、`mypy`、`lint-imports` 全绿；`make contracts-drift` up to date；`make contracts-generate` 后工作树干净 |
| 06:19 | `CONTRACT_BASE_REF=origin/main make contracts-speech-changes` passed（含 1 条批准登记）；`contracts-http-changes` passed；`validate-contracts` 37 files passed；`gh pr checks 701` 全绿（run `43512540554`，head 正是 `57df4e90`） |

---

## 十条逐条判定

### 1. 收窄是否正确且完整 —— 成立

- `contracts/speech-openapi.yaml` 的 `components.schemas.TtsJobInput.oneOf[0].properties.text`：`maxLength: 200000 → 4096`，`minLength: 1` 未动。
- 与同步入口一致：`SpeechRequest.properties.input.maxLength` 实测同为 **4096**、`minLength` 同为 **1**。
- 描述写清了口径：「A Job performs exactly one speech synthesis, so the body bound is the same as `input` on `/v1/audio/speech`; longer text is rejected instead of being split, truncated, or silently shortened.」同时 `paths./v1/nexus/jobs.post` 的 description 补齐了拒绝语义（形状不符 → 422 `unsupported_parameter` 且不写 Job 行；超过接收上限 → 400 `invalid_job_request`）。
- 不一致面已收口：`docs/architecture/speech-api.md` 同步为当前事实（含「长文切分合成是独立产品能力、上游按单元提交」），实测无第二处描述异步 TTS 正文上限的文档。
- 残留的 200000 都属别处字段：`contracts/workbench-openapi.yaml` 与 `RenderUnit*.text`（作者在「已知限制」里单列，属另一产品决定）。

### 2. 生成物是否由生成命令产出 —— 成立（实跑）

- `make contracts-drift` → `Contract bindings: up to date`。
- `make contracts-generate` 主动重写后 `git status --short` 为空 → 三处生成物（`file.schema.json`、`speech.generated.ts`、`source-inventory.json`）与生成命令输出逐字节一致，无法是手改。
- 生成物 diff 与 YAML 语义对应：`file.schema.json` 的 `maxLength` 与新增 description、`speech.generated.ts` 的 `TtsJobInput` 描述注释、`source-inventory.json` 的 `speech-openapi.yaml` sha256 同步变化；无额外手工补丁。

### 3. 一致性守卫是否真有牙 —— 成立（实跑 + 独立回滚）

守卫断言三层同源，读码即在：异步 `==` 同步（`minLength` 也断言）`==` 领域 `MAX_SPEECH_TEXT_CHARS` `==` 网关 `MAX_TTS_TEXT_CHARS`，另有 4096 原样进准入、越界返回契约声明的 422 信封、且 `jobs.submitted == []`。

独立回滚（**在 `/private/tmp/sn710-mut` 内，只改 YAML 的 4096→200000，不重生成**）：

```text
FAILED tests/contract/test_tts_text_bound_consistency.py::test_async_tts_body_bound_equals_the_synchronous_body_bound
FAILED tests/contract/test_tts_text_bound_consistency.py::test_declared_body_bounds_match_the_enforcing_limits
2 failed, 4 passed in 3.80s
```

与作者陈述的「不重生成时 2 failed」一致。守卫不是靠生成物自洽通过的：它同时钉住领域与网关常量，所以只改 YAML 也会红。

### 4. 消费者同步 —— 成立

- `web/workbench/src/file-workflow/TtsTaskInput.tsx:63`、`dev/TtsTaskDemo.tsx:101` 的 `maxLength` 均为 4096。
- `speech-file-client.ts:129` 注释改为「inline Job input text is bounded at 4096 characters, the same as `/v1/audio/speech`」。
- 真正的拦截不在这三处，而在 `file-task-session.ts` 的 `parse("JobCreateRequest", …)`：越界由生成契约校验器拒，发生在任何上传之前——作者新增的 `tts-task.test.ts` 两例（4097 / 200000）断言 `uploads == 0`、`createKeys == []`、`data.size == 0`，与实现路径吻合。
- 全仓 grep `200000`/`200_000`：代码与注释里已无「异步 Job 正文上限 200000」的过期表述；剩余命中为 `teleprompter/follow-session.ts`（本地 checkpoint 上限）、`render_units` 领域/DDL（另一字段）、作者新测试的字面量与 `docs/validation/` 的过程记录。

### 5. 登记类能否被滥用 —— **部分成立，存在一条真实越权通道**

「应该拒绝」的攻击面全部有牙（实跑，见下节 D–J、M4）：改旧值、改新值、放宽方向、缺 `proof`、指向别的 operation、契约回滚、finding 已不存在，全部 REJECT 并同时报出登记项自身的问题，不会静默。

但「换一个位置」的三组攻击全部静默通过（A / M2 / N）：登记项只自证**它点名的那个 component 的那一个 bound**，而被它移除的 finding 是**整个 operation 请求体的 `application/json/oneOf`**（内核 `_compare_schema` 在 `oneOf` 分支只发一条、随后 `continue`，不再向下递归；`_dereference_openapi` 又把 6 个变体全内联，所以 29 个可达 schema 的任意改动都归并到这一条 finding）。于是：

> 一条登记项把门从「这个 bound 变了」降级为「这个 operation 的请求体变了，其中一处是登记的收窄」。

这与类注释和 `docs/validation/http-contract-change-checks.md` 的声明直接冲突：两处都写「the service schema differs from the baseline by exactly that one bound, so the entry cannot hide a second simultaneous request change」。该保证只在单 component 内成立，在 operation 内不成立。

其余机制核对无误：

- **休眠**：基线/当前都是 4096 时既不报错也不放行任何东西；休眠状态下追加一处同级收窄仍然 REJECT（I 组）。
- **回滚**：基线/当前都 200000 时登记项变「未兑现」并 REJECT（J 组），与作者陈述一致。
- **版本作用域**：`(base_version, current_version)` 必须精确相等，否则条目完全不参与（作者的 `test_a_different_version_pair_still_rejects`）。
- **过滤了但没有 finding**：`exact finding … is absent` 分支有牙（M4 组）。

### 6. 「过滤了但没有对应 finding」是否真的报错 —— 成立

构造「登记的 200000→4096 与文档完全一致、但内核没有产出该 finding」的输入（把 kernel findings 置空）：

```text
FINDING: contracts/speech-openapi.yaml:paths//v1/nexus/jobs/post/requestBody: approved request
bound narrowing entry does not match: exact finding '…/requestBody/application/json/oneOf:
exclusive variants changed; compatibility needs explicit proof' is absent
GATE: REJECT
```

即：登记项不会在 finding 消失后变成永久静默豁免。

### 7. `_BOUND_DIRECTIONS` 方向表是否正确 —— 成立；缺 `maxContains` / `minContains`

实跑 `_bound_movement` 探针：

```text
maxLength:  4096 -> 2048  => 'narrowing'      maxLength: 2048 -> 4096   => 'relaxation'
minLength:  1 -> 2        => 'narrowing'      minLength: 2 -> 1         => 'relaxation'
exclusiveMaximum: 10 -> 5 => 'narrowing'      exclusiveMaximum: 5 -> 10 => 'relaxation'
exclusiveMinimum: 5 -> 10 => 'narrowing'      exclusiveMinimum: 10 -> 5 => 'relaxation'
maxLength: 4096 -> '4096' => None             maxLength: '4096' -> 2048 => None
maxLength: 4096 -> 4096.0 => None             maxLength: None -> 4096   => None
maxContains: 10 -> 5      => None             unknownBound: 10 -> 5     => None
maxLength: True -> False  => None
```

- `exclusiveMaximum` / `exclusiveMinimum` 方向正确。
- 非数值（字符串、布尔、缺失、`int` vs `float` 写法）一律判为「不是收窄」，因此无法被登记；把 `maxLength` 改成字符串 `"4096"` 时，它落入结构形状比较，登记项报「does not match the documents 200000 -> None」并 REJECT（L 组）。**不会误判**。
- 唯一缺口是**完备性**而非安全性：内核 `_constraint_break` 认识 `minContains`/`maxContains`，但 `_BOUND_DIRECTIONS` 没有收录，所以将来那两类收窄没有登记入口。方向保守（只是无法登记），但与该类自称的「请求值约束收窄」覆盖面不符。

### 8. 登记是否只作用于这一个 operation 的那一个字段 —— 成立（对别的 operation）

同文件另一个 operation 的同类收窄仍然报 finding（M3，把 `/v1/audio/speech` 的 `SpeechRequest.input.maxLength` 4096→2048）：

```text
FINDING: …/api/speech:paths//v1/audio/speech/post/requestBody/application/json/properties/input:
maxLength was tightened from 4096 to 2048
GATE: REJECT
```

登记项对该 operation 无影响。**注意**：本条只证明「跨 operation 不被覆盖」；**同一 operation 内的其它字段/其它变体是被覆盖的**（F1）。

### 9. 范围外改动是否恰当 —— 恰当

`tests/integration/test_public_job_admission_http_pg.py` 只改了三类内容，未顺手放宽任何其它断言：

- `CONTRACT_TEXT_CEILING` 200000 → `4_096`；
- 用例 3（4097）期望 `400 invalid_job_request` → `422 unsupported_parameter`，并同步 `kind`/`retryable` 期望；
- 模块 docstring、用例 docstring 与「已知边界」改写为当前事实（并保留 #703 的独立待办）。

关键点：`CONTRACT_TEXT_CEILING` 的唯一断言用途是 `assert len(text) <= CONTRACT_TEXT_CEILING`，改后从「4096 ≤ 200000（恒真）」变成「4096 ≤ 4096（有意义）」，是收紧而不是放松。422 的 `kind` 与生产映射一致：`backend/src/speechnexus_backend/speech/interfaces/http/job_errors.py:42` 为 `("unsupported_parameter": (422, "invalid_request_error", …))`。该文件是 PG 集成测试，本机 `disposable … PG probe not configured` 无法实跑；CI 的 `Integration 0–5` 全绿覆盖它。

### 10. 诚实度 —— 基本成立；一处正文陈旧、一处行为未被测试钉住

- **ruff B010（可核）**：`252c7aa0` 引入 `setattr(loaded, "_APPROVED_REQUEST_BOUND_NARROWINGS", registry)`，`006dd294` 改为 `vars(loaded)[...] = registry`。确有一次红→绿，且是换实现不是加 `noqa` 抑制（PR 内无新增抑制注释）。
- **`test_workbench_versions_postgres.py` 抖动（只能依赖作者陈述）**：本 PR 未触碰该文件（`git diff origin/main...HEAD --` 为空），本机因 PG probe 未配置只跑出 `16 passed, 20 skipped`，无法复现抖动；「同一 commit 重跑即过、main 同文件通过」只能采信作者与 CI。
- **PR 正文「验证」块陈旧**：该块自述取自中间 commit `cc47515c`，其中 `uv run pytest tests/contract -q：3703 passed`、`1819 formatted`、`1444 source files`、以及 `CONTRACT_BASE_REF=origin/main make contracts-speech-changes：**rejected**` 均非最终 head 事实。最终 head 实跑为 3718 passed、1822 formatted、1446 source files，且 `contracts-speech-changes` **passed**。建议在正文注明该块是登记类加入前的状态。
- **未列未验项**：`PublicSpeechRenderJobs.submit` 用 `parse("JobCreateRequest", body)` 本地校验，正文从 4097 起会从「远端 400 拒绝」变为「本地 `RenderSubmissionRejectedError`」。作者在「已知限制」里披露了这一点（异常类型相同、行为对用户等价），但现有测试只用超长 `reference`（`test_an_unrepresentable_unit_never_opens_a_connection`）覆盖本地拒绝，**没有用例用超限正文钉住新路径**。

---

## 必须修清单

### M1（阻塞，核心）：登记项的自证粒度必须与 finding 的过滤粒度一致

**现状**：`_apply_approved_request_bound_narrowings` 只核对 `component` 一个 schema；移除的却是该 operation 请求体的唯一一条 `application/json/oneOf` finding，而这条 finding 归并了 6 个变体、29 个可达 schema 的全部差异。

**影响**：一条登记项可以同时放行该 operation 请求体上任意一处未登记的同类改动——收窄（F1/A）、新增属性（F1/M2）、甚至放宽（F1/N）。放宽尤其危险：它让契约重新承诺服务给不出的能力，正是 #690 要消除的那类缺陷。三组反例在「无登记项」时都被拒，加回登记项后一律 `passed`。

**建议修法**（按侵入度从低到高，任选其一即可关闭）：

1. 在现有 component 自证之后，再要求该 operation 请求体的全部可达 schema 除登记项所点名的那个外**形状与 bound 集合都相同**：

   ```python
   reachable = _reachable_schemas(base, _request_body(base, path, method)) | _reachable_schemas(
       current, _request_body(current, path, method)
   )
   for name in reachable - {component}:
       base_schema = _service_schema(base, name)
       current_schema = _service_schema(current, name)
       if base_schema is None or current_schema is None:
           problem = f"reachable component {name} is missing on one side"
           break
       if not _kernel()._same_json_tree(base_schema[0], current_schema[0]) or _bound_differences(
           base_schema[1], current_schema[1]
       ):
           problem = f"another change exists in reachable component {name}"
           break
   ```

2. 或者把自证对象从 `component` 换成整个 `_request_body(...)` 的 `_bound_surface`，登记键改记绝对 pointer。

**配套必须做**：

- 补一条负例：同一 operation 的**同级变体**（不是同 component 的第二个属性）收窄/新增属性/放宽仍须 REJECT。现有 `test_a_second_simultaneous_bound_change_still_rejects` 只在同一 component 内加第二个 bound，而合成夹具 `document()` 的 `oneOf` 只有一个变体，正好绕开了这条通道——这是这次漏洞没被作者测试发现的直接原因。
- 修正两处过度声称：`check-http-contract-changes.py` 的类注释「the service schema differs from the baseline by exactly that one bound, so the entry cannot hide a second simultaneous request change」，以及 `docs/validation/http-contract-change-checks.md` 对应段落。若选方案 1/2 后仍希望保留操作级语义，也应改为准确表述。

### M2（建议，完备性）：`_BOUND_DIRECTIONS` 补 `maxContains` / `minContains`

内核的 `_constraint_break` 已把二者列为约束，登记表未收录，将来那两类收窄没有登记入口。方向安全，但与该类声明的覆盖面不符，建议补齐或在类注释里显式说明不含。

### M3（建议）：为 `PublicSpeechRenderJobs` 的本地拒绝补一条用例

用超限正文（如 4097 字符）断言 `RenderSubmissionRejectedError` 且 `calls == 0`，把「远端 400 → 本地拒绝」的行为改变钉住；否则这条披露过的行为没有任何回归保护。

### M4（建议）：PR 正文「验证」块补一句时点说明

该块取自 `cc47515c`，其中 `contracts-speech-changes` 的 rejected 与最终 head 相反。加一句「此块为登记类加入前的状态，最终 head 见 CI」即可，避免读者把红项当成当前结论。

---

## 独立复现的反例

全部在 `/private/tmp/sn710` 与 `/private/tmp/sn710-mut` 内进行，产品代码与契约零改动、零 push。攻击基座：用真实契约（base = `git show origin/main:contracts/speech-openapi.yaml`，current = PR head 的 YAML），走与 `inspect_sources` 相同的 `_compare_openapi(..., standard_only=False)` + `_apply_approved_request_bound_narrowings` 调用链。

### F1（核心缺陷）：把改动挪到同一 operation 的另一个变体，登记项静默放行

`VoicePreviewJobInput` 由 `VoicePreviewJobCreate → JobCreateRequest.oneOf` 从 `/v1/nexus/jobs` POST 可达，且没有第二个 operation 引到它。

三组变体（都额外叠加在真实 PR 的 YAML 上）：

| 组 | 额外改动 | 登记项 | 内核 findings | 结果 |
| --- | --- | --- | --- | --- |
| A | `VoicePreviewJobInput.text.maxLength: 2000 → 100`（未登记收窄） | 保留 | 1 条 `/v1/nexus/jobs/…/oneOf` | **PASS（静默接受）** |
| A′ | 同 A | 移除（`{}`） | 1 条同一 finding | REJECT |
| M2 | `VoicePreviewJobInput.properties.new_field` 新增（结构改动） | 保留 | 1 条同一 finding | **PASS（静默接受）** |
| N | `VoicePreviewJobInput.text.maxLength: 2000 → 5000`（未登记**放宽**） | 保留 | 1 条同一 finding | **PASS（静默接受）** |
| N′ | 同 N | 移除（`{}`） | 1 条同一 finding | REJECT |

真 CLI 复核（`/private/tmp/sn710-mut`，只把 `VoicePreviewJobInput.text.maxLength` 改成 100，其余保持 PR head）：

```text
$ CONTRACT_BASE_REF=origin/main uv run python scripts/dev-env/check-http-contract-changes.py --base origin/main --surface speech
HTTP structural baseline: 7147f306ebf2599673b923def20c759dc34cdf08
contracts/speech-openapi.yaml: 0.1.0.dev18 -> 0.1.0.dev18
  approved request bound narrowing: …/requestBody/TtsJobInput/oneOf/0/properties/text:
  request bound narrowed maxLength 200000 -> 4096 [#690: …]
Known HTTP structural checks: passed
$ echo $?
0
```

契约里此时存在一处**完全未登记、也未在 PR 描述里说明**的收窄，而门禁输出 `passed`。

### F2（预期拒绝，实测拒绝）：登记项自身的负例

均基于真实契约，只改登记项（不改契约）：

| 组 | 攻击 | 关键输出 | 结论 |
| --- | --- | --- | --- |
| D | 旧值改成 100000 | `declared maxLength 100000 -> 4096 does not match the documents 200000 -> 4096` + 原 finding 仍在 | REJECT |
| E | 新值改成 1000 | `declared maxLength 200000 -> 1000 does not match the documents 200000 -> 4096` | REJECT |
| F | 登记放宽方向 4096 → 200000 | `maxLength 4096 -> 200000 is not a narrowing; only a shrinking admitted set can be registered` | REJECT |
| G | 删掉 `proof` | `entry must include a non-empty issue, reason and proof` + 原 finding 仍在 | REJECT |
| H | 指向 `/v1/nexus/assets` | `TtsJobInput is not reachable from this operation request body` + 原 finding 仍在 | REJECT |
| I | 基线/当前都是 4096（休眠） | `approved: []`、`remaining: []`；休眠状态下再叠加同级收窄仍 REJECT | 符合预期 |
| J | 契约回滚（两边都 200000） | `declared maxLength 200000 -> 4096 does not match the documents 200000 -> 200000` | REJECT（未兑现即报错） |
| L | `maxLength: 4096` 改成字符串 `"4096"` | `declared … does not match the documents 200000 -> None` | REJECT（非数值不参与登记） |
| M3 | 同文件另一 operation：`SpeechRequest.input.maxLength 4096 → 2048` | `…/properties/input: maxLength was tightened from 4096 to 2048` 保留 | REJECT |
| M4 | 登记值与文档一致但 finding 不存在 | `exact finding '…' is absent` | REJECT |

---

## 本机复跑

环境：macOS arm64、Python 3.14.9（`uv sync --locked --all-packages --all-groups` + `pnpm install --frozen-lockfile`）、Node 26、worktree `/private/tmp/sn710` @ `57df4e90`。

| 命令 | 结果 |
| --- | --- |
| `uv run pytest tests/contract/test_tts_text_bound_consistency.py -q` | `6 passed in 0.59s` |
| 同上（`sn710-mut`，YAML 回滚 4096→200000） | `2 failed, 4 passed in 3.80s` |
| `uv run pytest tests/contract/test_approved_http_request_bound_narrowings.py -q` | `15 passed in 8.16s`（与作者「15 例」一致） |
| `uv run pytest tests/contract -q` | **`3718 passed in 148.83s`**（与作者一致；首次 44 collection errors 是我未装 workspace 包/node_modules，补齐后归零） |
| `make contracts-drift` | `Contract bindings: up to date` |
| `make contracts-generate` 后 `git status --short` | 空（生成物可由命令复现） |
| `CONTRACT_BASE_REF=origin/main make contracts-speech-changes` | `passed`，并打印 1 条 `approved request bound narrowing` |
| `CONTRACT_BASE_REF=origin/main make contracts-http-changes` | `passed` |
| `uv run python scripts/dev-env/validate-contracts.py` | `contract validation: passed (37 files)` |
| `uv run ruff check .` | `All checks passed!` |
| `uv run ruff format --check .` | `1822 files already formatted` |
| `uv run mypy --platform linux` | `Success: no issues found in 1446 source files` |
| `uv run lint-imports` | `Contracts: 9 kept, 0 broken.` |
| `cd web/workbench && pnpm test`（PR head） | `62 passed (62)` / **`773 passed (773)`** |
| 同上（`/private/tmp/sn710-main` @ `7147f306`） | `62 passed (62)` / **`770 passed (770)`** → +3 与新增用例数一致 |
| `gh pr checks 701` | 全 `pass`，2 项按路径 `skipping`；run `43512540554`，head 经 `gh pr view` 核对为 `57df4e90` |

> 备注：`scripts/dev-env/check-http-contract-changes.py:912` 使用 PEP 758 的无括号多异常写法（`except ValueError, OSError, …:`），在仓库要求的 Python 3.14 下合法；该行在 `origin/main` 已存在，不属本 PR。

---

## 只能依赖作者陈述或未实测

1. `tests/integration/test_public_job_admission_http_pg.py`（含 #700 同步改动）：本机 `disposable … PG probe not configured`，无法实跑；结论来自静态核对 + CI `Integration 0–5` 全绿。387 行的「零行 delta」「202 + Location」等断言强度未在本机复验。
2. `tests/integration/test_workbench_versions_postgres.py` 的「抖动」定性：本机 20 skipped，无法复现；只能确认该文件未被本 PR 触碰。
3. 绩效性/装配事实：CI run `43512540554` 的 16 项 pass、2 项 skipping 已用 `gh pr checks` 独立核对；但 CI 内部的覆盖数字（如 a11y、compose、deploy dry-run）未逐项打开日志核对。
4. 作者声称「重生成后守卫 5 failed」：我只复现了「不重生成 2 failed」，未复现另一半。
5. PR 正文「已知抖动记录」中关于 `assert None == '23503'`、`VersionStoreError('workbench store commit uncertain')` 的失败现场：无本地日志可核。

## 边界

- 未修改任何产品代码、契约、生成物或既有测试；本交付只新增本文件。
- 变异只在 `/private/tmp/sn710-mut` 与 `/private/tmp/sn710` 的临时探针中进行，未 commit、未 push；`uv.lock` 未纳入提交。
- 未合并、未 push main、未改动 PR #701 的任何状态或评论。
