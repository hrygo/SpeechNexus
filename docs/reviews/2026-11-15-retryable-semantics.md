# PR #807 非作者审查：`error.retryable` 语义固定（fix #806）

- 被审对象：PR #807 `fix/806-retryable-semantics`，head `7efb3c1ebe976ec3f2ab17b5ecea550971e39252`，基线 `origin/main` = `d90bf933e581bbed39ee0e42ab87e367b4e240d7`
- 审查方式：非作者审查；代码只读，变异仅在独立检出内施加并复原
- 检出：`/private/tmp/sn808-pr`（被审代码）、`/private/tmp/sn808-mut`（变异专用）、`/private/tmp/sn808`（本报告分支）
- 审查日期：2026-11-15

## 结论

**通过，无阻塞项。** 本卡最重要的两点均成立：

1. `check-openai-contract-compat.py` 的改动**没有削弱真实约束**。它只在 schema 位置剥离 `title`/`description`/`examples`/`$comment` 四个 annotation 键，其余键（`required`、`properties` 键名、`const`、`enum`、`pattern`、`type`、数值/长度边界、`dependentRequired`、`$ref`）仍逐字精确比较。七个方向对抗变异全部得到预期结论：约束改动红、annotation 改动绿，且经 `$ref` 内联进条件分支的共享定义同样被精确比较。
2. 前提**确实被钉住**。新增 68 个行为例经真实 `create_app` + 真实 router 分派，覆盖两个 router 全部 4xx `except` 分支；两个源级 pin 独立复现会红（加 `retryable=True` → 4 failed）。

同时纠正作者两处表述：反例 2 的「无门禁」结论成立（删 description 后仅新增 pin 变红，3810 passed），但**反例 3 的挡点并非只此一处**——除面板 `settled` 守卫外，后端附例与源级 pin 两层也会拦住同一改动（变异实测 4 failed）。

## 判定总表

| 项 | 内容 | 判定 | 证据 |
| --- | --- | --- | --- |
| A1 | 门禁脚本 diff 剥离范围 | 通过（附 1 处口径不精确） | 见 §1 |
| A2 | 四方向对抗变异 | 通过（4/4 预期） | 见 §1.2 |
| A3 | 文档承诺与 `oneOf` 先例 | 通过（均既有） | 见 §1.3 |
| B4 | 70 例是否经真实 app/router 驱动 | 通过 | 见 §2.1 |
| B5 | AST 断言独立复现 | 通过（4 failed） | 见 §2.2 |
| C6 | 删 description 无既有门禁变红 | 通过（仅新增 pin 红） | 见 §3 |
| D7 | 503 `retryable=True` 面板无变化 | 通过（挡点表述不精确） | 见 §4 |
| E8 | description 原文准确性 | 通过（2 处措辞张力） | 见 §5.1 |
| E9 | 例数、CI、审计表 | 通过 | 见 §5.2 |

---

## 1. 门禁脚本改动是否削弱真实约束（本卡核心）

### 1.1 diff 精读：剥离范围

改动位于 `scripts/dev-env/check-openai-contract-compat.py:1546-1585`。改动前 `if`/`then`/`else`/`not`/`contains`/`prefixItems`/`dependentRequired`/`dependentSchemas`/`unevaluatedProperties` 同处一个精确树循环；改动后：

- 第一组（`if`/`then`/`else`/`not`/`contains`/`prefixItems`）改为
  `_same_json_tree(_compatibility_shape(<branch>, standard_only=False), ...)`；
- 第二组（`dependentRequired`/`dependentSchemas`/`unevaluatedProperties`）保持 `_same_json_tree(<raw>, ...)`。

`_compatibility_shape`（`:712-754`）的剥离面是**唯一确定**的：递归进入 schema 位置时 `continue` 掉且仅掉 `{"title", "description", "examples", "$comment"}` 四键；`const`/`enum`/`default` 走 else 分支原样保留（注释明确 "const/enum contain data, not nested schemas"）。因此：

- **没有别的键被一起放过**：`required`、`properties` 的键名、`pattern`、`type`、`minimum`/`maxLength` 等边界、`dependentRequired`、`$ref` 都不在剥离集合内，逐字参与比较；
- **条件本身仍精确**：`if` 子树同样过 `_compatibility_shape`，剥完注释后由 `_same_json_tree` 做键集合与值递归相等判断，`numeric_equivalent=True` 只影响数字表示（`1` 与 `1.0`），与改动前一致；
- **字面数据不当注释**：`const: {"description": "before"}` 里的 `description` 是数据，保留；`properties` 下名为 `description` 的**属性名**是键，保留。作者自带的 `test_literal_data_that_names_an_annotation_key_is_still_compared` 覆盖这两点，本审查在 3811 例全绿中复现。
- **引用不会被跳过**：`_dereference_schema_children`（`:472-517`）在 `standard_only=False` 下按 schema 位置解析 `$ref`，`if`/`then`/`else` 在 singles 集合内，因此共享定义被**内联**进分支后再比较——这正是本就存在的假红来源，也是剥离注释后仍能抓到引用目标内约束改动的原因（§1.2 的 M5/M7 实测）。

一处**口径不精确**（不削弱，方向是偏严）：新注释与新增文档句称 `dependentSchemas` 「键是字面属性名……其树保持精确」，但 `dependentSchemas` 的**键**是属性名、**值**是 schema。实测（`standard_only=False`、合成文档）：

```
1) 顶层 dependentSchemas 只改子 schema 的 description  -> 报 finding（过严）
2) 同一改动放在 then 分支内的 dependentSchemas 子 schema -> 不报（被剥离）
3) 同一位置把 type object 改成 string                  -> 报 finding（正确）
```

即：顶层 `dependentSchemas`/`unevaluatedProperties` 的 annotation 仍会被判为「conditional or composed constraint changed」，而同一子树在 `if`/`then` 内却被剥离。二者都是 false positive 方向（拦得住、不放过），不构成门禁削弱；但文档把规则写成普适的「annotation at schema position」时，这条实现并不完全符合，同一份契约里两个位置的同一 annotation 改动会得到不同结论。

### 1.2 四方向对抗变异（实跑）

变异只在 `/private/tmp/sn808-mut` 施加，每次一个 hunk，跑完 `git restore`，收尾 `git status --porcelain` 为空。门禁为实际在 CI 中运行的两条命令：

- `uv run python scripts/dev-env/check-openai-contract-compat.py --base origin/main`
- `uv run python scripts/dev-env/check-http-contract-changes.py --base origin/main`

| # | 方向 | 变异（1 hunk） | http 门禁 | 结论 |
| --- | --- | --- | --- | --- |
| M1 | `then` 改 `const` 值 | `deletion.schema.json` `Deletion/allOf/0/then` `cleanup_state.const` `complete`→`done` | **红** `.../allOf/0/then` | 符合 |
| M2 | `then` 加 `required` | `Deletion/allOf/1/then` 增加 `required:["error"]` | **红** `.../allOf/1/then` | 符合 |
| M3 | `then` 改 `description` | `Deletion/allOf/1/then` 增加 `description` | **绿** | 符合 |
| M4 | `if` 条件改 `properties` 键名 | `Deletion/allOf/1/if` `status`→`state` | **红** `.../allOf/1/if` | 符合 |
| M5 | 引用目标内改约束 | `errors.schema.json` `ErrorBody.retryable.type` `boolean`→`string`（经 `then` 解引用） | **红** | 符合 |
| M6 | 引用目标内改描述 | `ErrorBody.retryable.description` 改写 | **绿** | 符合 |
| M7 | 引用目标内删保证 | `ErrorBody.required` 去掉 `retryable` | **红** | 符合 |

`check-openai-contract-compat.py` 在 M1-M7 上恒绿，因为它的比较面是 `_PUBLIC_CONTRACTS` 的 5 条 OpenAI 标准路径，`workbench-openapi.yaml` 与 `shared/deletion.schema.json` 不在其中——这不是放过，而是该门禁本就不看这些路径。为排除「改成 kernel 后 OpenAI 面也失去约束」，另做两条正反控制（`ErrorBody.allOf/0/then`）：

| # | 变异 | OpenAI 门禁 | 结论 |
| --- | --- | --- | --- |
| O1 | `allOf/0/then/required` 增加 `"code"` | **红**（`speech-openapi.yaml` 多个响应 `.../error/allOf/0/then`） | 约束仍精确 |
| O2 | `allOf/0/then` 增加 `description` | **绿**（http 门禁同绿） | 只放过注释 |

**必要性对照**（本 PR 要修的就是假红）：把内核回退到基线版本、保留本 PR 的契约 description，http 门禁报

```
- contracts/workbench-openapi.yaml:paths//v1/workbench/deletions/post/responses/202/application/json/allOf/1/then:
  conditional or composed constraint changed; compatibility needs explicit proof
- contracts/workbench-openapi.yaml:paths//v1/workbench/deletions/{deletion_id}/get/responses/200/application/json/allOf/1/then: ...
```

指针与作者描述一致：`ErrorBody` 经 `shared/deletion.schema.json:471` 的 `Deletion/allOf/1/then` 被引用。修前红、修后绿，且约束改动仍红——改动方向正确。

**结论：门禁未被削弱。** 七向变异与两个门禁的正反控制全部符合预期；唯一偏差是 `dependentSchemas`/`unevaluatedProperties` 的注释口径（偏严方向）。

### 1.3 文档承诺与先例是否既有

- 「annotation-only edits do not change acceptance」在基线文档中**已存在**：`git show origin/main:docs/validation/http-contract-change-checks.md:210`。本 PR 只是补齐条件分支的落地口径，不是新造理由。
- `oneOf` 的 `_compatibility_shape` 去注释是**既有先例**：基线内核 `:1623-1624` 已用 `_compatibility_shape(base_variants, standard_only=standard_only)` 比较 `oneOf` 变体，`_compatibility_shape` 在基线中共出现 10 次。本 PR 把同一规则延伸到条件/组合关键字，属同一策略的一致化。
- 未新增豁免登记、未重建基线：diff 中除两处循环外无其他逻辑改动，`tests/contract` 无新登记断言改动，未出现 baseline 类文件变更。

## 2. 前提是否真被钉住（本卡核心）

### 2.1 70 例是否经真实 `create_app` + 真实 router

是。`tests/unit/backend/test_workbench_export_assistant_refusal_retryable.py`：

- `_app()` 调 `create_app(None, assistant=..., exports=..., default_workspace_id=..., cursor_key=..., max_json_bytes=..., body_timeout_seconds=...)`（`:204`），请求经 `httpx.ASGITransport` 打真实应用、真实路由表（`:225-243`），不是直接调 router 函数；
- 只替换两处：注入型 `_RaisingService`（构造受测 4xx）与 `read_envelope`（文档已说明入口拒绝另由 `test_workbench_http_error_fallback` 覆盖）。受测面正是 router 自己的 `except` 分支，即决定 `retryable` 的位置；
- 例数：68 个参数化行为例 + 2 个源级/矩阵 pin = **70**（实跑 `70 passed in 0.90s`）。按 6 操作拆：`createAssistantTurn` 5+9=14，`getAssistantTurn` 4+9=13，`cancelAssistantTurn` 4+9=13，`createBookExport` 5+5=10，`getBookExport` 4+5=9，`cancelBookExport` 4+5=9。
- 4xx 分支覆盖完整性（本审查独立枚举 `except` 子句核对，不依赖作者陈述）：
  - `http_exports.py:97-115`：`IngressError`、`ResourceNotFoundError`、`IdempotencyConflictError`、`RevisionConflictError`、`ExportStateConflictError`、`InvalidVersionChangeError`，加 `VersionStoreError`(503)、`Exception`(500) 两个非 4xx。测试的 `EXPORT_BRANCHES` 与 4xx 集合**完全一致**；
  - `http_assistant.py:105-125`：上述外加上 `LlmError`、`AssistantStateConflictError`。`LLM_CODES` 8 码中 4 码为 4xx（`llm_invalid_input` 422、`llm_profile_mismatch` 422、`llm_source_not_sealed` 409、`llm_source_changed` 409），另 4 码为 503；测试覆盖的正是这 4 个 4xx 码，**无遗漏**；
  - 矩阵 pin 断言状态集合 `{400,404,408,409,413,415,422}`（`:270`），429 未进入是因为该状态只属 render 面（`_RETRYABLE_REFUSAL_CODES` 只含 `render_submission_*`），不属于这 6 个操作。

### 2.2 AST 断言独立复现

在 `http_exports.py` 的 `ResourceNotFoundError` 分支加 `retryable=True`（1 hunk），实跑：

```
FAILED ...test_workbench_export_assistant_refusal_retryable.py::test_every_4xx_refusal_is_terminal[createBookExport-404-resource_not_found]
FAILED ...::test_every_4xx_refusal_is_terminal[getBookExport-404-resource_not_found]
FAILED ...::test_every_4xx_refusal_is_terminal[cancelBookExport-404-resource_not_found]
FAILED ...::test_the_two_routers_never_pass_an_explicit_retry_permission
4 failed, 66 passed in 0.62s
```

与作者报告的 4 failed 一致，且证明两层都生效：3 个行为例说明 `_RaisingService` 确实驱动真实 router 分支（不是摆设），1 个源级 AST 断言覆盖「未来新增分支没被行为例覆盖」的盲区。

## 3. 反例 2：删 description 的「无门禁」结论

独立复现（`sn808-mut`，把 `contracts/shared/errors.schema.json` 回退到基线，即删掉 description 与空白差异，然后重派生生成物）：

- `check-openai-contract-compat.py`：`passed`；
- `check-http-contract-changes.py`：`passed`；
- `uv run pytest tests/contract -q`：**1 failed, 3810 passed**，唯一失败是新增的
  `test_error_retryable_semantics.py::test_the_shared_field_carries_the_same_dispatch_rule_and_its_two_limits`。

即：现有门禁对「删掉这条语义描述」完全不敏感，只有新增 pin 能拦住。作者的「无门禁→补 pin」判断成立。

注：不重派生生成物时该实验会额外出现 14 个 `contract() == generator().bundle_http_surface(...)` 之类的失败，那是源码与 `packages/contracts/**` 的机械 drift，不是语义断言；重派生后即消失。本次重派生需 `node`/`openapi-typescript`，签名：`36 errors (36 fixed)` / `Contract bindings: generated`。

## 4. 反例 3：503 `outcome_unknown` 改成 `retryable=True` 的挡点

作者结论「面板行为无变化」成立，但「挡点只此一处」不准确。实测三层：

1. **面板层（作者指出的挡点，成立）**：`recovery/command-journal.ts:61-64` 的
   `refusedCategory(settled, retryable)` 在 `!settled` 时直接返回 `null`，根本不读 `retryable`；两处调用点 `assistant-session.ts:253-260` 与 `books/book-export.ts:194-200` 的 `settled` 定义完全相同（`400<=status<500 && ∉{408,429}`），503 恒为 false。`command-journal.test.ts:186-194` 固定了 `refusedCategory(false,false)=null`、`(false,true)=null` 两行。
   本审查用真实模块执行（vitest 直跑生产 TS，非复述）：
   - status 400/409/503 × retryable false/true 组合中，503 与 500 无论 `retryable` 取何值，journal 分类均为 `null`、`refusedReplayable` 均为 `true`；
   - 单独 pin：`refusedCategory(false,true) === null`。
2. **后端声明层（作者未提，实测会红）**：把 `http_exports.py` 的 `VersionStoreError` 分支加上 `retryable=True` 后，真实断言应答变成
   `503 | x-should-retry: true | code: outcome_unknown | retryable: True`，并因与声明的 `const:false` 不符走「declaration-independent envelope」回退；`tests/unit/backend` 出现 4 failed——其中 3 个来自 `test_workbench_http_error_fallback.py` 的 `envelope()` 断言（`error["retryable"] is False`、`x-should-retry == "false"`），1 个来自 AST pin。即 `ERRORS`/声明层并不允许这个值。
3. **源级层**：`test_the_two_routers_never_pass_an_explicit_retry_permission` 直接禁止这两个 router 传 `retryable`。

因此准确表述是：**面板重放决定只由 `settled` 守着（唯一守卫，两处调用共享同一函数）；后端另有声明与源级两重拦截。** 作者「挡点只此一处」只对「面板行为」成立，对「改动能否落地」不成立。

补充（非阻塞）：`AssistantHost.tsx:219` 直接读 `turn.error.retryable` 渲染「（可重试）」文案，不经 `settled`。这是展示用途、不参与重放决定，但把字段值改成 true 会让失败回合的文案与「不可重放」结论相反。属文案层第二读者。

## 5. 契约与例数

### 5.1 description 原文准确性

- 「a 503 `outcome_unknown` is `retryable: false` and must still be replayed under the original Idempotency-Key」——与代码一致：`http_versions.py:144` 的 `outcome_unknown` 消息即「Preserve the original idempotency key; do not create a new intent.」；运行时该 503 的 `retryable` 为 false（实测应答），面板按 §4 重放。**准确**。
- 「Job 侧语义更窄……`execution_cleanup_pending` 是 false 而正确动作是同一 intent 同键重发」——与代码一致：`speech/interfaces/http/job_errors.py:143-148` 的 allow-list 只有 `job_outcome_unknown`/`job_capacity_exhausted`/`job_admission_contended`，`execution_cleanup_pending`(409) 因此为 false；workbench 侧 `http_render_units.py:88-92,144-149` 把同一上游码映射为 `render_submission_cleanup_pending` 并列入 `_RETRYABLE_REFUSAL_CODES`。**准确**。
- 同一 409 双态的例子成立：`_CONFLICT_CODES` 里 `idempotency_conflict`（不可重试）与 `execution_cleanup_pending`（可重试）同属一个操作状态族，正是 description 所说「一个操作的 409 可能要求等待清理、另一个 409 表示 key 已花」。
- 两处措辞张力（不阻塞）：
  1. `workbench-openapi.yaml:2283-2285` 新句「This operation answers `true` for that same wait, as `render_submission_cleanup_pending`」与同文件 `:2208-2215` 既有句「reserved rather than served……no dispatch answers with it today」需要一起读。代码侧 `_CONFLICT_CODES_RESERVED` 也明确该 disposition 今天不可达。字段语义描述本身没错（说的是已公布的 disposition），但「answers」易被读成运行时事实。
  2. 新测试 `test_every_4xx_refusal_is_terminal` 的 docstring/断言消息「A 4xx here rejects the same request forever, so the panel must not replay it」对矩阵中的 408 `request_timeout` 不成立：面板 `settled` 明确排除 408（`assistant-session.ts:251-257` 注释即「408 may have crossed the network」）并因此重放。被钉住的值（4xx 一律 false）符合 #806 要求，只是理由文案对 408 行不成立——这与本 PR 在 5xx 上强调的「值 false 不等于不可重放」是同一区分的遗漏。
- `ErrorBody` 结构未变：diff 只在 `retryable` 增加 `description`，`type: boolean`、`additionalProperties: false`、`required` 六项、`name`/`pattern` 等一字未改（`git diff origin/main...HEAD -- contracts/shared/errors.schema.json` 仅 3 行）。

### 5.2 例数与 CI

| 检查 | 本审查实测 | 作者陈述 | 结论 |
| --- | --- | --- | --- |
| `uv run pytest tests/contract -q` | `3811 passed in 146.36s` | 3811 | 一致 |
| `uv run pytest tests/unit/backend -q` | `3228 passed in 46.05s` | 3228 | 一致 |
| `uv run pytest tests/unit/test_operation_coverage.py -q` | `43 passed in 6.40s` | 43 | 一致 |
| `pnpm test`（web/workbench） | CI 日志 `Test Files 65 passed (65)` / `Tests 1063 passed (1063)` | 1063/65 | 一致 |
| `gh pr checks 807` | 全部 pass（含 `Python: tests, coverage and package build`、`Web: lint, types, tests, build and accessibility`、`Python: lint, types and contract compatibility`、6 个 Integration shard），head `7efb3c1e` | 全绿 | 一致 |

`tests/contract` 与本机环境有关：临时检出未安装 JS 依赖时，8 个 TypeScript 客户端用例会因 `ERR_MODULE_NOT_FOUND: openapi-fetch` 失败（`packages/contracts/src/workbench-http.ts`）；把工作区 `node_modules` 链接进来后即 3811 全绿。该失败是环境缺口，不是本 PR 缺陷。

审计表 `docs/audits/2026-10-26-team-c-operation-coverage.json` 是 `--write` 重派生而非手改：在检出内重跑
`uv run python scripts/dev-env/check-operation-coverage.py --write`（`{"written": ..., "operations": 52}`），与已提交内容的唯一差异是 `audited_head`（重派生时 HEAD 已是 `7efb3c1e`，提交内记录的是生成时的 `d90bf933`），`contract_sha256` 与 6 个操作的 `tests` 增项完全复现。

## 必须修清单

无阻塞（必须）修改项。以下 4 条建议在同 PR 或紧随其后的小 PR 修，均不影响本次合入的正确性：

1. **P3｜门禁口径与实现不一致**：`check-openai-contract-compat.py:1568-1585` 的注释与 `docs/validation/http-contract-change-checks.md` 新增段落称 `dependentSchemas` 因「键是字面属性名」而保持精确，但其**值**是 schema；实测顶层 `dependentSchemas`/`unevaluatedProperties` 内的 annotation 改动仍被判红，而同一子树在 `if`/`then` 内被剥离（§1.1 三例）。建议二者取一：把这两个键的**值**也过 `_compatibility_shape`（键名保持精确），或把文档改写成「只有 `dependentRequired` 的键是数据；`dependentSchemas`/`unevaluatedProperties` 的子树仍按精确树比较（偏严）」。
2. **P3｜新测试理由文案**：`tests/unit/backend/test_workbench_export_assistant_refusal_retryable.py` 的 docstring 与 `test_every_4xx_refusal_is_terminal` 的失败消息把「4xx」一律表述为「rejects the same request forever」，对矩阵内含的 408 `request_timeout` 不成立（面板正因 408 未结算而重放）。断言值保留，改文案即可。
3. **P3｜契约措辞**：`workbench-openapi.yaml:2283-2285` 的「This operation answers `true` for that same wait」与同文件「reserved rather than served」并存，建议改为「publishes that disposition as retryable（今天不可达，见上文 reserved）」，避免被读成运行时事实。
4. **P3｜反例 3 的挡点表述**：若后续文档/issue 复用本结论，应写成「面板由 `settled` 守卫；后端另有 `test_workbench_http_error_fallback` 的应答断言与源级 AST pin 两重拦截」（§4 实测 4 failed），避免被理解为只有一处保护。

## 只能依赖作者陈述的点

以下未在本机独立验证，仅采信作者陈述或间接证据：

1. **作者自称这些改动「超出仅契约+测试允许面」并主动请审**：本审查按代码事实判断（kernel 改动为共享内核、影响 5 个门禁脚本），未核对 #806 issue 正文对允许面的原始措辞。
2. **`pnpm test` 1063/65 的构成**：本机未在工作区安装 pnpm 依赖（临时检出无 `openapi-typescript`/`openapi-fetch`），只从该 head 的 CI 日志读取汇总数字，未本地重跑 65 个测试文件。
3. **集成层 6 个 PG/MySQL/MinIO shard 的实际覆盖**：只确认 `gh pr checks 807` 全绿，未在本地跑集成套件。
4. **`tests/contract` 的 3811 与 `tests/unit/backend` 的 3228 在 CI 上的同构性**：本地为该 head 的独立检出，CI 为同一 commit；未逐项比对两者用例集合是否完全一致。
5. **审计表 `audited_head` 的生成时序**：只能证明「重派生结果除 `audited_head` 外与提交一致」，无法证明作者当时确实运行的是 `--write` 而非等价手改（但内容与工具输出一致，实质上等价）。
6. **反例 1/2/3 的历史成因**（例如 #803 之前面板为何无 category）：本审查只验证当前树的事实，未复核历史 commit 的演进过程。

## 附录：实跑命令与关键输出

```bash
# 环境（两个检出各一次）
uv sync --locked --all-packages --all-groups

# 门禁 CLI 名称（先 --help 确认）
uv run python scripts/dev-env/check-openai-contract-compat.py --help   # --base BASE
uv run python scripts/dev-env/check-http-contract-changes.py --help    # --base/--format/--surface

# 基线（PR head + base=origin/main）
uv run python scripts/dev-env/check-openai-contract-compat.py --base origin/main
# -> OpenAI-compatible contract compatibility: passed
uv run python scripts/dev-env/check-http-contract-changes.py --base origin/main
# -> Known HTTP structural checks: passed

# 必要性对照：内核回退基线 + 保留 PR 契约
# -> 红：paths//v1/workbench/deletions/{...}/responses/{200,202}/.../allOf/1/then

# 七向变异（每次 1 hunk，跑完 git restore，git status 空）
# M1 red(allOf/0/then) M2 red(allOf/1/then) M3 green M4 red(allOf/1/if)
# M5 red M6 green M7 red ；O1 red（OpenAI 面）O2 green
```

```
tests/contract                                 3811 passed
tests/unit/backend                             3228 passed
tests/unit/test_operation_coverage.py            43 passed
web/workbench (CI log)                     1063 passed / 65 files
test_workbench_export_assistant_refusal_retryable.py   70 passed
```

```
# 反例 2（删 description + 重派生生成物）
uv run pytest tests/contract -q
# -> 1 failed, 3810 passed
#    only test_error_retryable_semantics.py::test_the_shared_field_carries...

# 反例 3（面板层，真实模块执行）
# 503/500 × retryable{false,true} -> category null, refusedReplayable true（两侧相同）
# 后端层（加 retryable=True）-> tests/unit/backend: 4 failed

# 审计表重派生
uv run python scripts/dev-env/check-operation-coverage.py --write
# -> {"written": "docs/audits/2026-10-26-team-c-operation-coverage.json", "operations": 52}
#    与提交内容差异仅 audited_head

# CI
gh pr checks 807   # 全部 pass，head 7efb3c1e
```
