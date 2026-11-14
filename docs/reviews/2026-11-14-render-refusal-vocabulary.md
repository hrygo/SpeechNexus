# 渲染拒绝词表与错误归因的非作者审查（PR #752 / issue #750）

- 审查对象：`fix/750-render-cause`，head `e1aca3637291a480cd836c12be11adccacb57417`（6 个提交：`a9db7847`、`c9ed4c61`、`e0794d1a`、`408225da`、`adb06a4d`、`e1aca363`）
- 审查方式：独立 worktree `/private/tmp/sn759`（`review/759-render-cause`，基于 `origin/main` `26fd8b39`）+ 被审内容的 detached 快照 `/private/tmp/sn759-pr`（`e1aca363`）。审查分支只承载本报告。
- 审查时间：2026-11-14（Asia/Shanghai）
- 被审内容与 `origin/main` 的关系：PR head **不包含** `origin/main` 顶端两个提交（`26fd8b39` #749 CI 门禁、`b521c9c0` #756 字数提示），两者均与本次改动路径无关；下列实跑全部在 `e1aca363` 的 detached 快照上完成，未改动 `origin/main` 内容。

## 结论

**结论：方向正确、可以准备合并，但"错误归因"与"两侧一致守卫"各留了一个洞，建议修完 3 项必须修再合并。**

这个 PR 解决的是真问题：原实现把上游 `{400,401,403,415,422}` 六类语义不同的拒绝折成一句无原因的 `RenderSubmissionRejectedError`，工作台只能猜。本 PR 让拒绝"保留能证明的原因、并且只声称能证明的原因"，三态 `RenderRejectionCause` 与 `RenderRejection.__post_init__`（`application/render_units.py`）把"没观测到的原因不许命名"做成了类型不变量，这一层是我看到的最扎实的部分。`#745` 的星形平面守卫、`#723` 的 409 结算语义都未回归。

但有两处需要修：

1. **两侧一致守卫不是真正双向的**——它比较的是"后端 `ERRORS` 的码、面板码表、契约正文"这三侧，**没有比较映射表 `_REFUSAL_CODES` 的取值**。我给 `_REFUSAL_CODES` 加一条指向不存在码的新映射后，词表 4 条断言、路由/adapter 60 条用例**全绿**，而真实路由抛 `KeyError` → 500。这恰是本卡最核心的守卫目标（"后端加了、别处没跟上"），只是漏在值这一层。
2. **映射在真实上游行为下仍会产生一次错误归因**——`unsupported_parameter` 的文本门要求 `param ∈ {input.text, text}`，但上游 `job_errors.py:164` 恒发 `param: None`（我复核了全部发射点），于是门在生产不可达；上游把"正文超过一次合成上限"也报成 `unsupported_parameter`（`contracts/speech-openapi.yaml` 的 `TtsJobInput` 描述自述）。默认部署里本地先用同一份 4096 契约拒掉，所以平时不触发；一旦工作台契约包与上游部署版本偏差（正是本 PR 注释里承认存在的 `#702`/`#703` 漂移），超界正文会被说成"核对钉住的声音/模型/绑定修订"，把一个文本问题指向固定字段。这不是"没有原因"，而是**错误原因**，比兜底更糟。

另有 1 项文案级必须修：`render_submission_workspace_unavailable` 让用户"切换到另一个工作区"，但派发请求体 `RenderUnitGenerationRequest` 只有 `expected_unit_revision`，服务端固定用 `default_workspace_id`，面板没有工作区选择——这个下一步在本操作里不可执行。

## 十三条逐条判定

| # | 条目 | 判定 |
| --- | --- | --- |
| A1 | `unsupported_parameter` 文本门 + 本地探针 | **部分不通过**：探针结论成立且有兜底，但真实上游 `param` 恒 null 使文本门不可达，版本偏差时错投 `field_unsupported` |
| A2 | `workspace_not_configured` / `workspace_unavailable` 拆分 | **拆分正确、文案错误**：上游两码确实分别对应"缺配额 → 运维"与"不存在/停用"，但"切换工作区"在本操作不可执行 |
| A3 | `model_binding_not_published` / `profile_not_published` 共用 `binding_unavailable` | **通过（有保留）**：两个资源在工作台都没有发布操作，两边唯一可执行的步骤都只剩"改钉"，合并未把两种不同的下一步说成一种 |
| A4 | 兜底码是否诚实 | **通过**：后端兜底不声称原因、不降级为未知、各分支文案不归咎文本；前端对**未声明码**的回退另有问题（见必须修/建议 4） |
| B5 | `upstream_unreadable` 的"4xx 确定未建 Job" | **成立（限定范围）**：拒绝集内 6 个状态码的 4xx 都由准入前抛出/事务回滚，无已提交 Job；但该结论是契约级而非代码级证明，且 429 被反向归成"未知" |
| C6 | 类型层守卫 `Record<RenderRefusalCode, string>` | **强，复现成立**：漏写文案 `tsc` 红（TS2741） |
| C7 | 枚举层守卫扫三侧 | **强，复现成立**：后端加码 3 红、面板删码 1 红 |
| C8 | 反向：前端多码 / 契约漏码 | **补测通过**：前端多码 1 红、契约漏码 1 红、契约多码 1 红；**但映射值方向不红**（见必须修 1） |
| D9 | HTTP 端到端用例是否走真实链路 | **通过**：真实 ASGI 路由 + 真实 `WorkbenchRenderUnits` + 真实 `PublicSpeechRenderJobs` + 真实映射，非手拼响应 |
| D10 | `#745` 星形平面守卫不回归 | **通过**：2049 码点 + 422 不归咎文本，`#745` 19 条全绿 |
| D11 | `#723` 409 结算语义 | **通过**：409 分支与取消/结算路径未改动 |
| E12 | 不动共享 `ErrorBody` 的结论 | **可靠**：`contracts/shared/` 零改动，`ErrorBody` 属性/required 两版完全一致；复算加 optional 属性 → `contracts-compat` 38 findings（与作者数字一致）、`contracts-http-changes` 557（作者 549，同类大量红）；PR 自身两门禁全绿 |
| E13 | 范围与 operation-coverage 刷新 | **通过（锚点有瑕疵）**：无 `uv.lock`/`pnpm-lock.yaml`/CI/deploy 改动；审计摘要仅改 2 个锚点字段且刷新必要；但 `audited_head` 落后一个提交 |

### A1. `unsupported_parameter` 的文本门与本地探针

三条分支实跑（真实适配器 → 真实映射）：

| 场景 | 适配器结论 | 工作台码 |
| --- | --- | --- |
| 本地 4097 码点（`_local_rejection` 探针换短文本后重校验成功） | `local_contract`, param=`input.text` | `render_submission_text_too_long` |
| 本地 4097 码点 **+** `client_reference` 超长（混合坏字段） | `local_contract`, param=`None` | `render_submission_rejected` |
| 上游 `422 unsupported_parameter`, `param=null`（**真实上游形状**） | `upstream_code`, param=`None` | **`render_submission_field_unsupported`** |
| 上游 `422 unsupported_parameter`, `param="input.text"` | `upstream_code`, param=`input.text` | `render_submission_text_too_long` |
| 上游 `422 unsupported_parameter`, `param="text"` | `upstream_code`, param=`text` | `render_submission_text_too_long` |

- **探针是否"只能因文本而拒"**：是，且在混合坏字段时会正确退回兜底（作者的说法核实成立）。机制是"把 `input` 整体换成 `{text: "synthetic"}` 后重解析同一份 body"；因为 `TtsJobInput` 目前对 `text` 只有 `minLength: 1`/`maxLength: 4096`，而单元文本域约束保证非空，"只有文本被拒 ⇒ 是长度"这一推理当前成立。**前提是文本 schema 只有长度约束**；将来若给 `text` 加 `pattern`，探针会把 pattern 失败说成 `text_too_long`。docstring 已写明前提，但没有任何断言把前提钉住。
- **上游真出现文本超界会怎样**：上游 `_create` 对 `parse("JobCreateRequest")` 失败一律回 `422 unsupported_parameter`（`interfaces/http/jobs.py:464-467`），且 `job_error()` 恒写 `"param": None`（`job_errors.py:164`；我用 grep 复核了 `speech/interfaces/http/` 下所有 `"param"` 发射点，job 相关全部为 `None`）。所以上游文本超界 → `unsupported_parameter` + `param: null` → **`field_unsupported`**，前端文案是"派发请求里有字段取值不被接受（例如本单元钉住的声音、模型或绑定修订与契约不符）。下一步：核对这些固定字段"。这就是错误归因。
- 可达性：本地 `parse("JobCreateRequest")` 与上游 `TtsJobInput` 来自同一份 `contracts/speech-openapi.yaml`（都是 `maxLength: 4096`），所以默认部署不会触发；触发条件是工作台契约包与上游部署版本不一致（上游把上限降低，或未来上游开始发非 null `param`）。作者在 `_REFUSAL_CODES` 上方的注释已承认 `#702`/`#703` 会持续调整"哪个上游码陈述哪个事实"——正是这种漂移。
- 与之配套的前端救援只在 `render_submission_rejected` 上生效（`RenderUnitPanel.tsx:177-180`）：`textOverDispatch` 只对兜底码按本地测量命名文本。所以 `field_unsupported` 这条错路没有 UI 层兜底。

### A2. 两个工作区码的拆分

上游 `_ERRORS`（`speech/interfaces/http/job_errors.py:53-63`）：`workspace_not_found`（"The workspace was not found."）、`workspace_disabled`（"The workspace is disabled."）、`workspace_not_configured`（"no provisioned Job quota"，注释明说"no retry can produce one"）。前者是两个不同的"工作区不可用"事实，后者是运维配额缺失——**拆分方向正确**，上游码与下一步动作确实不同。

问题在文案的**动作主体**：`render_submission_workspace_unavailable` 的下一步写成"切换到另一个工作区后重新派发"（`http_versions.py:171-175`、`render-refusals.ts` 同文案）。但本操作：

- 派发请求体 `RenderUnitGenerationRequest` 只有 `expected_unit_revision`（无 `workspace_id`）；
- 服务端路由所有操作都用服务端配置的 `default_workspace_id`（`interfaces/http_render_units.py` 全量传入），适配器构造的上游 Job 体也不带 `workspace_id`；
- 工作台前端没有工作区选择器（`web/workbench/src` 里没有工作区选择 UI）。

所以对默认工作区拿到 `workspace_not_found`/`workspace_disabled` 时，用户没有任何可执行动作；真实动作是运维修复部署配置（建/启用工作区行）。这与本 PR 反对的"把原因说错"是同一类错误，只是发生在动作主体上。

### A3. `binding_unavailable` 的合并

两个上游码：`model_binding_not_published`（"The selected binding revision is not published."）与 `profile_not_published`（"The profile revision is not published."），共用 `render_submission_binding_unavailable`，文案同时点名两种资源并给两个动作："发布该修订，或改钉到已发布的修订"。

核实"两种下一步是否同类"：

- 工作台合同里没有任何绑定/预设发布操作（`docs/audits/2026-10-26-team-c-operation-coverage.json` 的 52 个操作里 profile/binding 发布数为 0）；
- 但 `RenderUnitPatchV2` 允许改钉 `model_binding`、`voice`、`profile`、`model`，所以"改钉到已发布的修订"对两种资源**都**可执行；
- "发布该修订"对两种资源都需要工作台之外的入口。

结论：两边可执行的那一半完全相同（改钉），文案也没有把两者说成同一个资源，合并成立。唯一的保留是"发布"这个前置动作对用户同样不可达——与 A2 同源，但这里有可执行的第二半，不构成错误归因。

### A4. 兜底码的诚实性

后端兜底 `render_submission_rejected` 覆盖三类（本地非文本拒绝、上游信封不可读、上游码未映射），文案："请求被拒绝，且服务端没有给出本页能据以指示下一步的原因。下一步：核对单元文本与固定字段后重做；重复提交不会改变结果。"

- 不说"结果未知"✓；不归咎文本（文案只说"核对文本与固定字段"这两个**可能**项，不指认）✓；
- "重复提交不会改变结果"：被纳入拒绝集的 6 个状态码在上游都是确定性拒绝（`retryable` 仅 503/429 为真），成立 ✓；
- 未映射的上游码（我实测 `model_binding_retired`、`model_binding_mismatch`、`model_not_published`、`job_binding_incomplete`、`job_text_asset_unsupported`、`invalid_job_request`）都落到兜底，且这些都是"可用一步说明"的码（如 `model_binding_retired` 的下一步就是改钉）。兜底不说错话，但**词表的完整性距离"按用户下一步分组"的目标还有一段**：`model_binding_retired`/`model_binding_mismatch` 与已映射的 `binding_unavailable`/`field_unsupported` 其实是同一个动作。
- **前端另有一处不诚实**（本 PR 引入的路径）：若 422 携带本页未声明的码，`problemOf` 走 `return { key: "unknown" }`（`RenderUnitPanel.tsx:185`），显示"结果尚未确认。请核对连接后按原键重试，不要重新创建。"——把一次**拒绝**说成**结果未知**。作者在测试里把它写成"runtime backlog"，但这与本 PR 自己"拒绝不得降级为结果未知"的原则冲突，且已有现成的通用拒绝文案可用。

### B5. `upstream_unreadable` 的"4xx 确定未建 Job"

- 拒绝集 `{400,401,403,413,415,422}`：上游 `_create`（`interfaces/http/jobs.py:446-505`）在 `service.submit()` 之前就返回 `job_request_too_large`/`invalid_job_request`/`unsupported_parameter`，之后的所有 4xx 异常（`UnknownWorkspaceError`、`DisabledWorkspaceError`、`ModelNotPublishedError`、`ModelBindingError`、`VoiceNotPublishedError`、`ProfileNotPublishedError`、`PronunciationNotFoundError`、`AssetNotReadyError`）都是准入抛出；`workspace_not_configured` 与 `job_capacity_exhausted` 由 `infrastructure/public_job_admission.py:183/212` 在**同一准入事务**内抛出（文档亦声明"不写 Job 行"）。因此拒绝集内**没有**"可能已建 Job 的 4xx"。
- 需要注意边界：这是**契约/实现级**结论，不是代码级证明（适配器无法区分"上游拒绝"与"中间代理伪造的同类状态码"）；且 `409`（可能带已存在的 Job）被单独走 `RenderSubmissionConflictError`，`429 job_capacity_exhausted` 被归为"结果未知"——后者同样确定没有建 Job，方向与本 PR 把 413 纳入拒绝集相反。这是既有边界（本 PR 未扩大），建议另开卡而不是在本卡改。

### C6–C8. 两侧一致守卫的真实强度

复现（全部在临时副本上做，每次 `git checkout --` 复原，结束 `git status --porcelain` 为空）：

| 变异 | 期望 | 实测 |
| --- | --- | --- |
| M5（C6）：`RENDER_REFUSAL_CODES` 加码、不写 `RENDER_REFUSAL_STEPS` | `typecheck` 红 | `error TS2741: Property 'render_submission_copy_missing' is missing in type ... but required in type 'Record<...>'` |
| M1（C7-A）：后端 `ERRORS` 加码、面板不动 | 红 | `3 failed, 1 passed`（`…agree_on_the_vocabulary`、`…contract_states_the_same_vocabulary`、`…operator_step…`） |
| M2（C7-C）：面板删一个码、后端不动 | 红 | `1 failed, 3 passed`（`…agree_on_the_vocabulary`） |
| M3（C8）：面板加一个后端没有的码 | 红 | `1 failed, 3 passed`（`…agree_on_the_vocabulary`） |
| M4（C8）：契约正文删一个码 | 红 | `1 failed, 3 passed`（`…contract_states_the_same_vocabulary`） |
| M7（C8）：契约正文多一个后端没有的码 | 红 | `1 failed, 3 passed`（`…contract_states_the_same_vocabulary`） |
| M6：把 `_REFUSAL_CODES["voice_not_published"]` 的值改成拼错 | 应有失败 | 词表 4 条**全绿**；路由级 2 条红（`test_a_refusal_reports_the_operator_step_it_demands[rejection5-…]`、`test_a_coded_refusal_reaches_the_route_as_a_workbench_code[422-voice_not_published-…]`）——即映射取值靠路由用例兜，不靠词表守卫 |
| M8：给 `_REFUSAL_CODES` **新增**一条取值不存在的映射（`"model_binding_retired": "render_submission_binding_retired"`） | 应红 | 词表 4 条**全绿**、路由/adapter 60 条**全绿**，真实路由抛 `KeyError 'render_submission_binding_retired'`（即 500） |

所以：**码集合层面是双向的**（后端↔面板↔契约正文三向都能红），**映射取值层面是单向且开放的**。M8 是这层守卫真正的缺口：它正是"新增一个映射"最常见的方式，而它不会让任何守卫变红。

其他可维护性观察：

- `_contract_codes()` 只扫 `generateRenderUnitV2` 的 `description`，这是设计（契约正文单点陈述），但意味着同一 description 里出现**合法的**历史码举例（如"旧服务可能返回 `render_submission_legacy`"）也会红——是刻意的严格，不是缺陷。
- `test_every_code_has_an_operator_step_and_the_panel_uses_the_list` 对面板的运行时使用只做 `"refusedCode(" in PANEL` 的字符串存在性检查，属弱断言；面板行为由 vitest 的 `it.each(RENDER_REFUSAL_DISPOSITIONS)`（9 条）兜住，整体不弱。

### D9–D11. 链路与既有语义

- **D9**：`test_a_coded_refusal_reaches_the_route_as_a_workbench_code` 的 9 条参数化 + 不可读兜底 + 本地超界，用的都是 `create_app`（真实 ASGI 路由）→ `_Service` 包装的真实 `WorkbenchRenderUnits` → 真实 `PublicSpeechRenderJobs`（仅 HTTP transport 为 `MockTransport`）→ 真实 `refusal_code` 映射。`-v` 实跑 28 条全绿，9 条参数化逐个可见。不是手拼响应。唯一瑕疵是跨测试模块导入私有 helper（`tests.unit.backend.test_workbench_render_dispatch._Store`、`test_public_speech_render_jobs.upstream_error`）。
- **D10**：`render unit text bounds (#745)` 19 条全绿，含 `counts code points, so astral text the server accepts is not called over-bound`、`keeps a full 4096 astral code points dispatchable`、`does not blame the text when an astral unit under the bound gets a 422`（422 + 2049 码点 → 通用拒绝文案，`title="render_submission_rejected"`，不含"超过一次派发上限"/"该单元文本"）。
- **D11**：`git diff` 中 409 相关行零改动（适配器 `response.status_code == 409` 分支与 `RenderSubmissionConflictError` 原样；路由仍映射 `idempotency_conflict`），`#723` 的取消/终态结算路径（`render_units.py` 的取消与 reconcile 部分）未被触碰。

### E12. 不动共享 `ErrorBody` 的结论

- `git diff origin/main...e1aca363 -- contracts/shared/` 为空；`ErrorBody` 两版本的 `properties`（`code,message,nexus,param,request_id,retry_after_ms,retryable,type`）、`required`、`additionalProperties: false` 完全一致。
- 生成的契约包只改了**描述文本**（`workbench-http.schema.json` 的 `RenderUnitText.description`、`workbench.openapi.json`/`workbench.generated.ts` 的 operation description），无结构变化——即 (c) 路线确实没碰 schema。
- 复算作者数字：给 `ErrorBody` 加一个 optional 属性（`upstream_code`）后，`CONTRACT_BASE_REF=origin/main make contracts-compat` → **38 findings**（与作者一致）；`make contracts-http-changes` → **557 findings**（作者 549；我换 `type:string` 与 `type:[string,null]` 两种写法都是 557，差异来自属性子 schema/基线细节，量级与结论一致）。两个 gate 均 exit 1。PR 自身内容下这两个 gate **全绿**。

### E13. 范围与 operation-coverage 刷新

- 改动 21 个文件，落在 `backend/src/speechnexus_backend/workbench/{application,infrastructure,interfaces}`、`contracts/workbench-openapi.yaml`、`docs/architecture/speech-api.md`、`docs/audits/…json`、`packages/contracts`（生成物）、`tests/{contract,unit/backend}`、`web/workbench/src/books`。无 `uv.lock`、无 `pnpm-lock.yaml`、无 `.github/**`、无 `deploy/**`。
- 审计摘要 `--write` 刷新是否必要：`contracts/workbench-openapi.yaml` 的 sha256 从 `17ea2b48…`（origin/main）变为 `612993bd…`，不刷新就会被 `contract_drift` 判红——必要。diff 只改 `contract_sha256` 与 `audited_head` 两个字段——只更新锚点。
- 锚点瑕疵：head 上 `audited_head = 408225da`，但 `408225da` 的契约 hash 是 `f9c05ed6…`，而审计摘要记的是 `612993bd…`（= `adb06a4d` 起的 hash）。即 `--write` 是在"工作区已改、尚未提交 `adb06a4d`"时跑的，`audited_head` 落后一个提交一个事实。`differences()` 不比较 `audited_head`，所以门禁不受影响（`operation-coverage-check` 输出 `findings: []`），但该字段的 provenance 不实。

## 「映射是否产生错误归因」

**默认部署：不产生。** 因为本地契约与上游契约共用同一份 `maxLength: 4096`，本地先拒（`local_contract` + `input.text` → `text_too_long`），上游看不到超界正文；其余六类码的归因都正确（我逐码实跑：`workspace_not_configured` → 运维配额；`workspace_not_found`/`workspace_disabled` → 工作区不可用；`voice_not_published` → 发布/改钉；`asset_not_ready` → 等就绪/重传；`model_binding_not_published`/`profile_not_published` → 发布/改钉；`413` + `job_request_too_large` → 缩短正文/调上限；不可读信封 → 兜底不声称原因）。

**版本偏差时：会产生一次错误归因。** 上游对"正文超界"回 `422 unsupported_parameter` 且 `param` 恒 `null`，映射落到 `field_unsupported`，前端让用户"核对钉住的声音、模型或绑定修订"。同时 `_INLINE_TEXT_PARAMS` 这道防线在上游开始发非 null `param` 之前是**不可达分支**（两条参数化用例是合成的，不是上游真实形状）。

**两处需要修正的归因/动作**：

1. `field_unsupported` 的文案把下一步收窄到固定字段（必须修 2）；
2. `workspace_unavailable` 让用户"切换工作区"，而本操作没有工作区选择（必须修 3）。

## 「两侧一致守卫是否双向」

**码集合层面：双向，且我逐向复现了红。** 后端加码→红，面板删码→红，面板加码→红，契约删码→红，契约加码→红；类型层缺文案→`tsc` 红。

**映射取值层面：单向开放。** 新增一条 `_REFUSAL_CODES` 取值指向不存在的码时，词表守卫与全部路由用例保持全绿，运行时 `KeyError` → 500（M8）。这是"这个对扫"真正的缺口：它保护了"三侧列了哪些码"，没保护"映射能输出哪些码"。修法很小（见必须修 1）。

## 必须修清单

1. **（守卫，核心）把映射取值钉到 `ERRORS`。** 在 `tests/contract/test_render_refusal_vocabulary.py::test_the_mapping_only_names_codes_the_upstream_contract_declares` 增加 `set(_REFUSAL_CODES.values()) <= set(ERRORS)`，并加一条全覆盖断言：对 `_ERRORS` 中每一个拒绝状态码（400/401/403/413/415/422）构造 `RenderRejection.upstream(...)`，断言 `refusal_code()` 的返回值 ∈ `ERRORS`。证据：M8（全绿 + `KeyError`）。
2. **（归因，核心）`unsupported_parameter` + `param: null` 不得只指向固定字段。** 二选一：(a) 改文案为"请求里有字段取值不被接受，**包括正文超出上游当前接受的上限**；下一步：核对钉住的声音/模型/绑定修订，并确认正文未超上游上限"；(b) 把 `param` 为 null 的 `unsupported_parameter` 归入兜底 `render_submission_rejected`，只在 `param ∈ {input.text, text}` 时给 `text_too_long`。同时把 `_INLINE_TEXT_PARAMS` 标注为"上游开始指名 `param` 后可达"，避免把合成用例当成真实上游行为的证明。
3. **（动作主体）`render_submission_workspace_unavailable` 的下一步改为本操作可执行的动作。** 派发请求无 `workspace_id`、服务端固定 `default_workspace_id`、面板无工作区选择，因此"切换到另一个工作区"不可执行；应改为"该部署的目标工作区不存在或已停用，需要运维修复"。

## 建议（非阻断）

4. 前端对**未声明的 4xx（含 422）**回退到 `unknown`("结果尚未确认")，与"拒绝不降级为结果未知"冲突；本操作的全部 422 都是拒绝，可直接回退到通用拒绝文案，一行改动。
5. 已发布但本操作不可达的码：`render_submission_asset_unavailable`（适配器从不发送 asset/asset_id）、`render_submission_body_too_large`（派生体 ≈13 KB，默认接收上限 262144 B）、`text_too_long` 的上游分支（本地先拒）。建议在契约里注明这些是本操作的保留码，或说明保留原因；否则消费者分支永远没有生产验证。
6. `--write` 在脏工作区执行会让 `audited_head` 落后一个提交（本次已发生）。建议 `--write` 拒绝脏树，或同时记录 `dirty: true`。
7. 探针的"只可能因长度而拒"是外延证明；建议在契约测试里钉住前提（`TtsJobInput.text` 只有 `minLength`/`maxLength`，无 `pattern`/`enum`），否则将来加约束会把 pattern 失败说成 `text_too_long`。
8. 词表完整性：`model_binding_retired`、`model_binding_mismatch`、`model_not_published`、`job_binding_incomplete`、`job_text_asset_unsupported` 与已映射码动作相同却落兜底，和"按用户下一步分组"的目标有距离；可另开卡（不阻断本卡）。
9. 测试跨模块导入私有 helper（`_Store`、`upstream_error`），建议移到 `tests/support/` 或就地定义，降低改测试时的耦合。
10. 429 `job_capacity_exhausted` 仍归"结果未知"（确定未建 Job、可重试），与本 PR 把 413 纳入拒绝集方向相反；属既有边界，建议另开卡。

## 独立复现的证据

环境：系统 node `v26.12.1`（mise shim）、Python `3.14.9`（`uv sync --locked --all-packages --all-groups`）、pnpm `12.14.0`。全部在 `/private/tmp/sn759-pr`（detached `e1aca363`）执行；本机未起 MinIO，unit/contract 用例不依赖它。

### 门禁实跑（全部绿）

```
tests/contract         3753 passed in 158.40s          # 作者 3753，一致
tests/unit/backend     3089 passed in 43.87s           # 作者 3108，差 19（--collect-only 也是 3089）
web/workbench vitest   63 files / 897 passed           # 作者 897，一致
pnpm test:behavior     4 passed (2.2s)                 # 先 pnpm build
pnpm test:a11y         52 passed (18.5s)               # 首次与 behavior 并发跑，端口 4173/4174 strictPort 冲突失败；串行重跑绿
pnpm typecheck         Done (packages/contracts, web/shared, web/admin, web/workbench)
make contracts-drift           Contract bindings: up to date (exit 0)
make contracts-check           contract validation: passed (37 files)
make contracts-route-conformance  OK: every registered speech route matches the contract
CONTRACT_BASE_REF=origin/main make contracts-compat        OpenAI-compatible contract compatibility: passed
CONTRACT_BASE_REF=origin/main make contracts-http-changes  Known HTTP structural checks: passed
CONTRACT_BASE_REF=origin/main make contracts-speech-changes Known HTTP structural checks: passed
make operation-coverage-check  {"operations": 52, "bound": 42, "pending": 10, "findings": []}
ruff check .            All checks passed!
ruff format --check .   1850 files already formatted
mypy --platform linux   Success: no issues found in 1453 source files
lint-imports            Contracts: 9 kept, 0 broken.
```

`tests/unit/backend` 的 19 条差额未能解释：`--collect-only -q` 同样收 3089，`tests/unit` 全集是 6921，`origin/main` 比 PR head 多的两个提交不含 pytest 用例。按"只能依赖作者陈述"记录，不视为缺陷。

### A1/B5 探测（真实适配器 → 真实映射）

```
case                                                      adapter                                            workbench code                          param
A1-1 local text 4097 cp                                   local_contract param=input.text                     render_submission_text_too_long         input.text
A1-1b local text 4097 + over-long client_reference        local_contract param=None                           render_submission_rejected              None
A1-2 upstream unsupported_parameter param=null (real)     upstream_code 422 unsupported_parameter p=None      render_submission_field_unsupported     None
A1-2b upstream unsupported_parameter param=input.text     upstream_code 422 unsupported_parameter p=input.text render_submission_text_too_long         input.text
A1-2c upstream unsupported_parameter param=text           upstream_code 422 unsupported_parameter p=text      render_submission_text_too_long         text
A2 workspace_not_configured                               upstream_code 422                                   render_submission_workspace_not_configured  None
A2 workspace_not_found                                    upstream_code 422                                   render_submission_workspace_unavailable     None
A2 workspace_disabled                                     upstream_code 422                                   render_submission_workspace_unavailable     None
A3 voice_not_published                                    upstream_code 422 param=voice.revision              render_submission_voice_unavailable     voice.revision
A3 binding_not_published                                  upstream_code 422 param=model_binding.revision      render_submission_binding_unavailable   model_binding.revision
A3 profile_not_published                                  upstream_code 422 param=profile.revision            render_submission_binding_unavailable   profile.revision
A3b model_binding_retired (real 422, unmapped)            upstream_code 422                                   render_submission_rejected              None
A3b model_binding_mismatch (real 422, unmapped)           upstream_code 422                                   render_submission_rejected              None
A3b model_not_published (real 422, unmapped)              upstream_code 422                                   render_submission_rejected              None
A3b job_binding_incomplete (real 422, unmapped)           upstream_code 422                                   render_submission_rejected              None
A3b job_text_asset_unsupported (real 422, unmapped)       upstream_code 422                                   render_submission_rejected              None
A3b invalid_job_request (real 400, unmapped)              upstream_code 400                                   render_submission_rejected              None
A5 413 job_request_too_large                              upstream_code 413                                   render_submission_body_too_large        None
A5 413 with an unrelated readable code                    upstream_code 413 voice_not_published              render_submission_body_too_large        None
A5 unreadable 422 proxy page                              upstream_unreadable 422                             render_submission_rejected              None
B5 429 job_capacity_exhausted                             UNKNOWN(not a refusal)
B5 404 on create                                          UNKNOWN(not a refusal)
B5 409 idempotency_conflict                               RenderSubmissionConflictError(not a refusal)
```

（`413` + 无关可读码被状态码优先级盖成 `body_too_large`，属可接受的保守，但记录在案。）

### 变异复现（每次改完 `git checkout --` 复原，收尾 `git status --porcelain` 为空）

```
M5  C6：list a code, omit its operator copy
    src/books/render-refusals.ts(45,14): error TS2741: Property
    'render_submission_copy_missing' is missing in type ... but required in type 'Record<...>'
M1  C7-A：backend adds a code
    3 failed, 1 passed
M2  C7-C：panel deletes a code
    1 failed, 3 passed
M3  C8 reverse：panel adds a code the backend never emits
    1 failed, 3 passed
M4  C8 reverse：contract drops one code from the operation prose
    1 failed, 3 passed
M7  C8 reverse：contract invents a code the backend never emits
    1 failed, 3 passed
M6  mapping value typo
    vocabulary 4 passed; route tests 2 failed
M8  new mapping value that no other side declares
    vocabulary 4 passed; route+adapter 60 passed; real route -> RAISED KeyError 'render_submission_binding_retired'
```

### D9 / D10

```
tests/unit/backend/test_workbench_render_generation_http.py   28 passed
  ├ test_a_coded_refusal_reaches_the_route_as_a_workbench_code[422-unsupported_parameter-input.text-…]
  ├ … 9 条参数化逐条可见 …
  ├ test_an_unreadable_upstream_refusal_stays_the_unnamed_fallback
  └ test_a_locally_over_bound_unit_is_named_by_the_route_with_no_request
RenderUnitPanel.test.tsx -t "text bounds"                     19 passed | 27 skipped
  ├ counts code points, so astral text the server accepts is not called over-bound
  ├ does not blame the text when an astral unit under the bound gets a 422
  └ states the operator step for <9 codes>
```

### E12 复算

```
contracts/shared/ 在 PR 中改动：无
ErrorBody properties  @origin/main == @e1aca363：code,message,nexus,param,request_id,retry_after_ms,retryable,type
给 ErrorBody 加 optional upstream_code 后：
  contracts-compat         38 findings   （作者 38，一致）
  contracts-http-changes   557 findings  （作者 549，量级与结论一致）
  （PR 自身内容下两门禁均 exit 0）
```

## 过程记录（时间线）

1. 建 worktree：`git fetch` → `git worktree add /private/tmp/sn759 -b review/759-render-cause origin/main`；另建 detached 快照 `/private/tmp/sn759-pr`（`e1aca363`）用于读取与实跑，保证审查分支不含任何产品文件。
2. 读 diff（21 文件 / +1030 −54）：发现 `except A, B:` 无括号元组写法，先核对 `requires-python = ">=3.14,<3.15"` 与仓库既有同写法，确认是 PEP 758，非缺陷（避免误报）。
3. `uv sync` / `pnpm install --frozen-lockfile`（均命中缓存，<2s）。
4. 跑两大 pytest 套件、vitest、typecheck、build；再串行跑 behavior / a11y（首次并发跑 a11y 因端口冲突失败，属我的调度错误，已串行重跑绿）。
5. 跑 6 个 contract gate、operation-coverage、ruff、mypy、lint-imports。
6. 写 A1/B5 探测脚本（真实适配器 + 真实映射，22 个场景），得到上面两张表。
7. 写变异 harness（M1–M8），逐条复现并复原；M8 发现映射取值缺口。
8. 复算 E12 的 38/557，核实 `contracts/shared` 未动与 `ErrorBody` 两版一致。
9. 核对 A2/A3 的可执行性：派发请求体 schema、`default_workspace_id` 路由全量传入、工作台无工作区选择、无 binding/profile 发布操作、`RenderUnitPatchV2` 可改钉、前端 `WorkbenchResponseError` 不带 `param`。
10. 复核 B5 上游发射点（`jobs.py` 的 `_create` 顺序、`public_job_admission.py` 的配额/容量抛出、`job_errors.py` 的 `"param": None`）。
11. 复原 `contracts/shared/errors.schema.json`，确认工作树干净，写本报告。

## 只能依赖作者陈述 / 未验证的点

- **本机无 MinIO**，未跑依赖对象存储的集成路径；本卡改动不涉及 MinIO，判定不受影响。
- **上游真实部署行为**：`unsupported_parameter` 的 `param` 恒 `null` 是我从本仓库 `job_errors.py:164` 与 `_create` 发射点复核的**代码事实**，但我没有连到真实上游部署抓包；作者"上游恒发 `param: null`"的实测我按同源代码接受。
- `contracts-http-changes` 的 549 → 我的 557 差异未逐条归因（属性子 schema/基线细节），只确认量级与"两门禁必红"的结论。
- `tests/unit/backend` 的 3108 → 我收 3089（差 19），未定位作者计数来源。
- `test_a11y` 首次失败是我并发调度的端口冲突，不是 PR 问题；串行 52 通过。
