# 一次合成文本上限对齐的非作者审查（PR #737）

> 过程记录文档：保留时间线、独立复现的命令与输出。结论面向合并决定；未修改产品代码、契约与测试，变异全部施加在临时快照 `/private/tmp/sn740-pr` 并已还原。
>
> 审查对象：PR #737 `fix/704-705-text-limit`，head `ab92c3eed61046f2536e5ab6870c2793919d696f`（下称 head），修 issue #704 + #705。被审 head 的基线为 `1ef3c61319d20a90d114a04f57115acf83a0d755`（下称 base）；审查当日 `origin/main` = `d58127c02eac437913b051be77eec973115a1489`（下称 main，比 base 多 2 个纯文档提交 #735/#736）。
>
> 审查分支 `review/740-text-limit` 基于 `origin/main` 拉出（`git worktree add /private/tmp/sn740 -b review/740-text-limit origin/main`）。被审内容用 `git show ab92c3ee:<path>` 与 detached 快照 `/private/tmp/sn740-pr` 读取，审查分支上未检出被审改动，变异只在快照里施加并逐条还原。

## 结论

**三个判断都成立，测量全部可独立复现，守卫可红；但新增文案里有 1 处指向不服务的操作，必须改。判定：小修后合并。**

- **#704 判 B 成立**：`RenderUnit.text` 确实进入同一条合成路径，且 4097 在本机（0 请求）就被公开 Job 契约拒绝，不是静默截断、不是越界通道；因此不需要把存储上界收紧到 4096。**200000 应当保留**（理由与实测见下节）。
- **#705 判 A 不成立、B 成立**：两个码确实是各自入口的「整类形状拒绝」桶，实测同步 4 类非法请求全 400 / 异步 4 类全 422；把正文超限单独改成同一个码，会在一个本来就按整类归一的口径上开特例，且不会消除其他形状差异。
- **必须修 1 处（MF-1）**：新增的 `RenderUnitText` 描述与 `docs/architecture/speech-api.md` §1.4 都把拒绝方写成 `generateRenderUnit`（v1 operationId），但 v1 派发路由**没有注册**——服务端只装 `generateRenderUnitV2`，同一 PR 的 §1.6 对照表写的又是 v2 路径。契约里的指针指到一个取不到的路由，与「把关系写清」的目标正相反。
- 另有 4 条守卫/措辞建议（S-1~S-4，不阻断合并）：对照表的 `param` 与上限列没有守卫（单点变异全绿）；新增的第 7 处同类字段不会被发现（测试枚举 6 个固定 schema 名而非扫描）；「一次合成受 4096 限制」把**内联请求体上限**写成了**合成能力上界**，当前成立但 #696 落地即失效。
- 本 PR **没有任何产品代码或运行时行为改动**：`git diff --name-only base head` 的 15 个文件全在 `contracts/`、`docs/`、`packages/contracts/`（生成物）与 `tests/`。

## #704 判 B 是否正确、200000 该不该保留（本卡核心）

### 判 B 是否正确 —— **成立，但要显式说明这是对 issue 二分规则的重解释**

issue #704 的分支条件是「**如果 RenderUnit 不经过同一条合成路径** → B：记录两条路径的不同上界与原因（例如分章多单元）」；反之走 A（收敛到 4096）。作者实测的却是「**经过同一条路径，且越界被拒**」。也就是说，作者选的 B 不是 issue 字面上的 B 条件，而是第三条结论：*同一路径、显式拒绝、不存在不可交付承诺*。

这个重解释在技术上是对的，理由有三条，都能实测：

1. **不存在不可交付的承诺。** 派发前由公开 Job 契约同一道校验拒绝，越界文本一个字节都不会外发（4097 → 0 请求），因此 200000 不构成「契约允许、实现做不到」的那种不一致。
2. **A 的收益只是「提前到保存期拒绝」，代价是跨层下沉 + 数据迁移。** 把 `render_units.text` 的 CHECK、领域 `_text`、契约 `maxLength` 一起压到 4096，等于让**存储层**承担**一次请求的形状**约束：既要求对存量行做迁移，也取消了「先存长片段、再按单元切分」的编辑形态。
3. **A 也不能消除不一致，只能把差异搬到一个更窄的层面。** 一次请求的正文上界与存储容量本来是两个量；把两者强行相等后，后续任何长文能力（#696）都要再改一次 CHECK 与迁移。

但**这条重解释必须显式写在 PR/issue 结论里**：issue #704 的验收标准写的是「A 或 B 落地」，而字面上触发 A 的条件（「确实走同一合成限制」）已经被作者的实测命中。作者在 PR 正文给了「单元是编辑与溯源边界，收紧会失去保存章节片段的能力」这一理由，够用；建议把「实测命中的是 A 的条件，但 A 的收益仅为提前拒绝、代价是跨层下沉与迁移，故按 issue 的『记录不同上界与原因』执行」这句话补进 issue #704 的收尾说明，由 issue owner 确认 AC 判定口径，否则字面 AC 无法自证满足。

### 200000 该不该保留 —— **应当保留**

我把整条链路端到端跑了一遍（真实工作台 HTTP + 真实派发适配器 + 真实公开语音 ASGI 应用，监听真实端口；隔离 disposable PG），关键数据：

```text
unit 200001 chars -> 400 {"error":{"code":"invalid_request",...}}            # 契约形状拒绝（保存期）
unit 200000 chars -> 201 {"object":"nexus.render_unit",...}                  # 源容量可达、确实能存
dispatch 200000-char unit -> 422 {"error":{"code":"render_submission_rejected",...}}
speech app requests seen: []                                                 # 一个请求都没进语音底座
attempts after the refusal -> {"object":"nexus.list","data":[],"has_more":false}   # 无 attempt 行
patch to 4096 -> 200
dispatch after the edit -> 503 outcome_unknown（我的假准入抛错，属探针自身）
speech app requests seen: ['/v1/nexus/jobs']                                 # 改回 4096 后恢复派发
```

保留 200000 的理由：

1. **它是可达的真实容量，不是死信。** 200000 字符建单元返回 201、200001 返回 400，契约 / 领域（`_text(self.text, 200000)`）/ DDL（`length(text) <= 200000`）/ 测试四处同值。
2. **越界后没有留下脏状态，而且可恢复。** 422 之后 `render-units/{id}/generations` 仍为空（无 attempt 行、无 Job 行、无远端请求）；把正文改到 4096 再派发即恢复。存储层保留长正文不会制造「半成品」。
3. **上层文本本来就分两层容量。** 章节正文 `ChapterCreateRequest.text` 上限 5,000,000、转写 `TranscriptRevision.text` 1,000,000、文本资产 1,000,000 —— 仓库一贯把「字段容量」当 schema 容量而不是「一次请求可携带」的承诺（章节上限同样超过默认 1 MiB 请求体上界）。200000 与这套模型一致。
4. **收敛到 4096 属产品范围变更，不是一致性修复。** 一旦收紧，任何超过一次合成长度的片段都必须先切分；而产品今天**没有切分能力**（无 split 接口、无 UI 切分、长文合成 #696 未实现），收紧只会把「存不下的长文」变成「存都存不了」，并不增加安全。

保留的**前提**需要在后续卡里补上，属本 PR 范围外：当前工作台 `RenderUnitPanel.tsx` 的正文 `<textarea>` 没有 `maxLength`、也没有任何 4096 提示，派发被拒时只显示「请求被拒绝。请核对单元文本或声音后再重做。」——用户拿不到「上限 4096、需要切分」这条信息。也就是说「200000 只是源容量、不承诺可合成」这个语义目前只存在于契约与文档里，没有出现在用户可见处。建议单独开卡（或并入 #696）在编辑器提示 + 派发错误文案里表达该上界；不建议为此改动存储上界。

## 十一条逐条判定

### A. #704 的测量结论

#### 1. 「进同一条合成路径」是否成立 —— **成立，且确实没有第二条派发通道**

四类独立证据（都在 head 上 `rg`/路由枚举，非作者陈述）：

```console
$ rg -n "async def submit\(self, \*, unit" --glob '*.py' .
backend/src/.../workbench/application/render_units.py:59      # Protocol
backend/src/.../workbench/infrastructure/public_speech_render_jobs.py:45   # 唯一生产实现
tests/unit/backend/test_workbench_render_dispatch.py:96       # 测试替身
$ rg -n "\.submit\(" backend/src/speechnexus_backend/workbench/   # 唯一调用点
application/render_units.py:182:        job_id = await self._jobs.submit(unit=unit, key=key, reference=reference)
$ rg -n "render_jobs|PublicSpeechRenderJobs\(" backend/            # 唯一装配点
workbench/runtime.py:623:    render_jobs = None if client is None else PublicSpeechRenderJobs(client)
workbench/runtime.py:624:    render_units = WorkbenchRenderUnits(PostgresRenderUnits(versions), render_jobs)
$ rg -n "/v1/nexus/jobs" backend/src/                            # 唯一出站 POST
workbench/infrastructure/public_speech_render_jobs.py:69: response = await self._client.post("/v1/nexus/jobs", ...)
```

另外两处排查，用来排除「别的入口也能带走单元正文」：

- **前端不直连语音底座**：`web/workbench` 对 `/v1/nexus/jobs` 的调用全部集中在 `file-workflow/`（文件工作流，正文走文本资产 `uploadText`），渲染单元正文只经 `workbench-client` 打到工作台自己的 `render-units/{unit_id}/generations`。而 `tts` + `text_asset_id` 在生产准入里被显式拒绝（见下条），因此文件工作流并不构成「长正文单次合成」的旁路。
- **`OPERATIONS` 只装 v2 派发**（见第 7 条的附带发现）：v1 `generateRenderUnit` 连路由都没有，不存在「另一条路由也提交 Job」。

#### 2. 4097 是否真是「明确拒绝」—— **成立，0 请求是关键，我独立复现了两次口径**

口径一（真领域对象 + 真适配器 + MockTransport 计数，走真实用例 `WorkbenchRenderUnits.start_unit`）：

```text
MAX_SPEECH_TEXT_CHARS=4096
len=  4096  http_requests_sent=1  outcome=ACCEPTED job_id='job_aaaaaaaa...'
len=  4097  http_requests_sent=0  outcome=RenderSubmissionRejectedError
len= 20000  http_requests_sent=0  outcome=RenderSubmissionRejectedError
len=200000  http_requests_sent=0  outcome=RenderSubmissionRejectedError
--- 领域存储上界 ---
RenderDependencies(text=200000) accepted
RenderDependencies(text=200001) ValueError: invalid render text or identifier
```

口径二（同一适配器，但 httpx transport 直接是最真实的公开语音 ASGI 应用，4096 必须真的被它接收）：

```text
len=  4096  requests_reaching_speech_app=1  admission_calls=1  outcome=(我的假 Job 形状不全，走完准入)
len=  4097  requests_reaching_speech_app=0  admission_calls=0  outcome=RenderSubmissionRejectedError
len=200000  requests_reaching_speech_app=0  admission_calls=0  outcome=RenderSubmissionRejectedError
```

口径三（HTTP 端到端，见上节核心数据）：200000 单元派发 → 422 `render_submission_rejected`，语音应用侧 0 请求，`generations` 为空。三条口径一致：**越界在本地就被拒，越界文本从未发出**。适配器的 `submit` 不重试、不换键（`public_speech_render_jobs.py:1-6` 的 docstring 与实现一致），因此「0 请求」不可能是「重试后放弃」。

#### 3. 判 B / 保留 200000 是否正确 —— **见本卡核心两节：判 B 成立（需显式承认重解释了 issue 二分），200000 应保留。**

### B. #705 的结论

#### 4. 「入口级错误桶」是否成立 —— **成立；作者「第三个特例」的措辞偏松，但实质论证站得住**

实测（真 HTTP、装配后的应用，八个单点）：

```text
== SYNC /v1/audio/speech ==
     over_limit 4097: status=400 code='invalid_request' param='input'
       unknown_field: status=400 code='invalid_request' param='bogus'
          wrong_type: status=400 code='invalid_request' param='input'
            bad_enum: status=400 code='invalid_request' param='response_format'
     bad_speed_range: status=400 code='invalid_request' param='speed'
== ASYNC POST /v1/nexus/jobs ==
     over_limit 4097: status=422 code='unsupported_parameter' param=None
       unknown_field: status=422 code='unsupported_parameter' param=None
          wrong_type: status=422 code='unsupported_parameter' param=None
            bad_kind: status=422 code='unsupported_parameter' param=None
```

代码位置与作者描述一致：同步 `speech/interfaces/http/speech.py:226` 的 4096 校验经 `_text` 抛 `_InvalidRequestError`（`speech.py:375` 映射 400 `invalid_request`）；异步 `speech/interfaces/http/jobs.py:465-467` 的 `parse("JobCreateRequest", payload)` 失败 → 422 `unsupported_parameter`。

**需要修正的措辞**：「只改正文超限会造出第三个特例」不准确——同步入口**本来就有** 422 `unsupported_parameter`，只是轴不同：

```text
commit_mode=sentence (契约已声明但当前 binding 不支持): 422 unsupported_parameter param='nexus.overrides.tts.text_commit_mode'
commit_mode=planner  (契约未声明):                     400 invalid_request        param='nexus.overrides.tts.text_commit_mode'
Idempotency-Key 头（声明但无持久重放）:                422 unsupported_parameter param='Idempotency-Key'
```

所以准确的说法是：**该入口对「请求体形状不合法」只有 400 一个桶**（正文超限属于形状：`maxLength`），对「契约已声明但 binding 不支持」另有一个 422 桶；把正文超限从形状桶挪到 422 桶，等于让同一个字段的两种形状错误落在不同码上，并且仍不会消除「未知字段/类型错误」在两入口的码差。作者 A 不成立的结论仍然成立，只是理由要按这条改。

补充一条作者没写、但更硬的 A 不成立理由：`/v1/audio/speech` 是 OpenAI 兼容面（`scripts/dev-env/check-openai-contract-compat.py:36` 的标准路径之一），把该入口的形状拒绝码从 400 改成 422 会同时改掉兼容面的既有语义，属破坏性错误面变更。

#### 5. 对照表是否可执行 —— **可执行，逐行核对后 5 行实测一致、2 行仅静态核对；但表里两列没有守卫**

`docs/architecture/speech-api.md` §1.6 六行逐行核对：

| 行 | 实测/核对 | 结果 |
| --- | --- | --- |
| `/v1/audio/speech` 正文 400 `invalid_request` `param: input` retryable 否 | 实测（上表 + 声明信封校验） | 一致 |
| `/v1/nexus/jobs` 正文 422 `unsupported_parameter` `param` 无 retryable 否 | 实测 `param=None` | 一致 |
| `/v1/nexus/jobs` 请求体字节 413 `job_request_too_large`（默认 262144 B） | 实测 300125 B 请求 → 413 该码；`settings.py:174` 默认 262144 | 一致 |
| `/v1/nexus/assets` 上传 4 MiB → 413 `asset_too_large` | 静态：`settings.py:57` 默认 4 MiB（且 `le` 也只能 4 MiB）、`asset_errors.py:20` | 一致（缺 MinIO 未实跑） |
| `/v1/nexus/assets` 文本资产 1000000 字符 → 422 `asset_text_too_long` | 静态：`domain/assets.py:55` 默认 1000000、`asset_errors.py:19` | 一致（缺 MinIO 未实跑） |
| `POST /v2/workbench/render-units/{unit_id}/generations` 422 `render_submission_rejected` | 实测（HTTP 端到端 422 + 该码；`http_versions.py:151` 声明 422 / retryable False / param None） | 一致 |

结论：消费者按这张表写分支是可行的——同步侧靠 `error.code` + `param == "input"`，异步侧靠 `error.code` + `param == None`，其余四行只需状态码。**但**表的 `param` 与「声明上限」两列没有任何守卫（见第 6 条的 G1 探针：把同步行的 `input` 改成 `无`、4096 改成 8192，全部测试仍绿）。

### C. 守卫强度

#### 6. 反例独立性 —— **成立：作者 3 条全部独立复现，红在预期断言；我另加 3 条（M4/M5/M6）。但 M2 的红来自既有测试，不是本 PR 新增**

按 `docs/agent-verification.md` 的单点计量要求，每个变异单独施加、单独运行（`git diff -U0 HEAD` 的 hunk 数只作下界），在快照里跑 4 个相关测试文件（35 例）：

| 变异（单点） | hunk | 失败数 | 红在 |
| --- | --- | --- | --- |
| M1 契约 `RenderUnitText.maxLength 200000 → 4096` | 1 | 3 | `test_every_render_unit_text_site_shares_one_declared_source_bound`、`..._matches_the_domain_capacity`、`..._matches_the_stored_check_constraint` |
| M2 适配器静默截断 `dependencies.text[:4096]` | 1 | 1 | `test_public_speech_render_jobs.py::test_text_above_the_job_bound_is_rejected_locally_before_any_connection`（`DID NOT RAISE`） |
| M3 删掉 `RenderUnitText` 的描述块 | 1 | 1 | `test_the_source_bound_is_documented_as_distinct_from_one_synthesis` |
| M4 六处里第一处改回内联 `type/minLength/maxLength` | 1 | 1 | `test_every_render_unit_text_site_shares_one_declared_source_bound` |
| M5 DDL `length(text) <= 200000 → 4096` | 1 | 1 | `test_the_declared_source_capacity_matches_the_stored_check_constraint` |
| M6 同步超限改答 422 `unsupported_parameter`（「统一码」方向） | 1 | 1 | `test_tts_text_bound_consistency.py::test_the_same_over_bound_body_keeps_each_entry_point_code`（`assert 422 == 400`） |

六次还原后 `git status --porcelain` 均为空。作者 PR 正文的三条反例我都复现了，数值差异只是运行范围不同（作者跑单文件，我跑 4 文件）。**M2 的守卫是本 PR 之前就存在的**（`test_public_speech_render_jobs.py` 未在 diff 中），本 PR 自己新增的测试不覆盖适配器截断——这条很重要，见 S-4。

另复现了作者对 #705 的两条反例细节：只删同步描述的交叉引用句 → `test_both_entries_document_one_limit_and_their_two_codes` 红在 `assert "422" in description`；只删对照表的异步行 → `test_the_cross_entry_bound_table_states_each_entry_code` 红。

**未覆盖的两处（我加做的缺口探针，均单点、均全绿）**：

- **G1**：把对照表同步行的 `param` 单元格 `input → 无`、`4096 字符 → 8192 字符` → `9 passed`。即表里消费者最需要的两列（该带哪个 `param`、上限是多少）无守卫。
- **G4**：往契约里加第 7 个 `RenderUnitDraft`（内联 `maxLength: 200000` 的 `text`）→ `4 passed`。即测试用的是 6 个写死的 schema 名，不是「凡承载单元源文本的 schema 都必须复用该组件」的规则式断言。

#### 7. `RenderUnitText` 是否覆盖全 —— **成立：6/6 全部换成 `$ref`，没有第 7 处**

```console
$ python3 - <<'PY'  # 枚举所有声明 text 属性的 schema 与全部 $ref
  TranscriptRevision: text -> maxLength 1000000
  TranscriptRevisionCreateRequest: text -> maxLength 1000000
  MeetingSealRequest: text -> maxLength 1000000
  ChapterCreateRequest: text -> maxLength 5000000, minLength 1
  RenderUnit:               text -> #/components/schemas/RenderUnitText
  RenderUnitCreateRequest:  text -> #/components/schemas/RenderUnitText
  RenderUnitPatch:          text -> #/components/schemas/RenderUnitText
  RenderUnitV2:             text -> #/components/schemas/RenderUnitText
  RenderUnitCreateRequestV2:text -> #/components/schemas/RenderUnitText
  RenderUnitPatchV2:        text -> #/components/schemas/RenderUnitText
$ # 全部 $ref 出现处（6 处）
RenderUnit / RenderUnitCreateRequest / RenderUnitPatch / RenderUnitV2 /
RenderUnitCreateRequestV2 / RenderUnitPatchV2 的 properties.text
```

`RenderUnit*` 家族其余 schema 均不承载正文：`RenderUnitList`/`RenderUnitListV2` 是列表包装、`RenderUnitGenerationRequest` 只有 `expected_unit_revision`、`RenderUnitGenerationAccepted(V2)` 回执、`RenderUnitGenerationListV2` 是历史列表。生成物侧同步一致：`workbench_models.py` 6 处 `RenderUnitText`、`workbench.generated.ts` 6 处、三个 JSON schema 各 6 处，无手改痕迹（`make contracts-drift` 复算通过）。

附带发现（**先于本 PR 的契约/实现漂移，不属本 PR 责任**）：契约声明了 v1 `POST /v1/workbench/render-units/{unit_id}/generations`（operationId `generateRenderUnit`），但 `http_render_units.py:43-46` 的 `OPERATIONS` 只装 `generateRenderUnitV2`，`create_render_unit_router` 实际注册的路由也没有该 v1 POST；v1 `createRenderUnit` 则注册了但显式 `workbench_unavailable`。`docs/validation/http-contract-change-checks.md` 已把「未实现操作按声明比较」写成已知口径，所以结构检查放行。这条漂移本身不在本 PR 范围，但它使 MF-1 成立（见下）。

#### 8. 新契约测试断言的层次 —— **断言源契约文本，不是生成物；DDL 同源确实做到了**

- `tests/contract/test_render_unit_text_bound.py` 直接 `yaml.safe_load(Path("contracts/workbench-openapi.yaml"))` 读**源契约**，断言组件本身、6 处 `$ref`、描述语义；不读任何生成产物，因此改生成物不会误绿、改源契约必然红（M1/M3/M4 已验证）。
- DDL 同源：`render_units_migration_sql("synthetic_schema")` 生成 `CREATE TABLE` 文本，断言 `length(text) <= {契约 maxLength}` 子串——即「建表用的同一份 SQL 构造器」而不是另抄一遍数字（M5 验证）。
- 领域同源：`RenderDependencies(text="中"*maxLength)` 必须被接受、`maxLength+1` 必须 `ValueError`——即契约容量 == 领域容量（M1 验证）。
- `tests/contract/test_tts_text_bound_consistency.py` 同样读源契约 + 真 HTTP 实测信封；对照表守卫读的是 `docs/architecture/speech-api.md` 的 markdown 行（文档即被测对象，符合 #705 的「只文档化时必须有测试防漂移」）。

### D. 范围与记录

#### 9. 两处契约热点同时改是否破坏既有审批/兼容检查 —— **没有，全部通过（实跑）**

```text
make contracts-drift                                        → Contract bindings: up to date
make contracts-check (validate-contracts)                   → contract validation: passed (37 files)
scripts/dev-env/check-route-conformance.py                  → OK: every registered speech route matches the contract
CONTRACT_BASE_REF=origin/main make contracts-speech-changes  → Known HTTP structural checks: passed
CONTRACT_BASE_REF=origin/main make contracts-http-changes    → Known HTTP structural checks: passed
CONTRACT_BASE_REF=origin/main make contracts-compat          → OpenAI-compatible contract compatibility: passed
```

补充：两个契约的版本号未变（speech `0.1.0.dev18`、workbench `0.1.0.dev2`），因为本 PR 只增补描述/`$ref` 抽取，属非结构性变化；结构检查按基线 `d58127c0` 比对通过。`contracts-drift` 需要 `node_modules`（`normalize-typescript-headers.mjs` 依赖 `openapi-typescript`），未装依赖时该目标会以 `ERR_MODULE_NOT_FOUND` 失败——这是环境前置，不是本 PR 的问题。

#### 10. operation-coverage 摘要刷新是否必要 —— **必要，且刷新内容只有 2 个字段**

必要性单点实测：把映射文件换回 base 版本再跑检查 →

```text
{"operations": 52, "bound": 42, "pending": 10,
 "findings": [{"code": "contract_drift", "id": "scope", "detail": "contract digest changed"}]}
```

换回 head 版本 →

```text
{"operations": 52, "bound": 42, "pending": 10, "findings": []}
```

即契约描述文本改变 → `contract_sha256` 变化 → 检查必然红，`--write` 是唯一合法路径（不是为了让检查变绿而重写基准）。该文件相对 base 的 diff 只有 `contract_sha256` 与 `audited_head` 两行；52 个 operation 的 binding/consumers/tests/status 一字未动（因此没有借刷新篡改既有证据）。新摘要 `17d4bc56…`、`54283b9e…` 与 head 上两个契约的实际 sha256 一致。`audited_head` 记的是该映射派生时的 head（`1ef3c613`，即 base），与该文件历史上的维护方式一致（上一版记 `15173b1a`，同样不是当时的 main 头），且检查不比较该字段，纯记录性。

另核实作者列的 `tests/unit/test_operation_coverage.py` → **15 passed**（与 PR 正文一致）。

#### 11. 范围 —— **干净；`uv.lock`/`pnpm-lock.yaml` 无改动**

`git diff --name-status base head` 恰 15 文件：`contracts/`（2）、`docs/architecture/speech-api.md`、`docs/audits/…operation-coverage.json`、`packages/contracts/`（7，全部生成物）、`tests/`（3）。没有 `backend/`、`web/`、`scripts/`、`deploy/` 改动，也没有 `uv.lock`/`pnpm-lock.yaml`/`package.json` 改动；`git diff --check` 干净。生成物确由 `make contracts-generate` 产出（`make contracts-drift` 复算一致，无手改）。

一条非缺陷的流程提示：被审 head 的 base 是 `1ef3c613`，比审查当日 `origin/main` 落后 2 个纯文档提交（#735/#736），合并前需 rebase/快进，无冲突面。

## 必须修清单

| 编号 | 位置 | 问题 | 修法 |
| --- | --- | --- | --- |
| **MF-1** | `contracts/workbench-openapi.yaml` 的 `RenderUnitText.description`；`docs/architecture/speech-api.md` §1.4 段 | 两处都把拒绝方写成 `generateRenderUnit`（v1 operationId），但 v1 派发路由**未注册**，只能装到 `generateRenderUnitV2`；同一 PR 的 §1.6 对照表写的却是 `POST /v2/...`。契约里的指针指向一个取不到的路由（v1 请求落到工作台通用 404 信封），与「让消费者一处查到正确入口」的目标相反 | 两处改成 `generateRenderUnitV2`，或写成「派发操作（`/v2/workbench/render-units/{unit_id}/generations`，`generateRenderUnitV2`）」；顺手让 `test_the_source_bound_is_documented_as_distinct_from_one_synthesis` 或 §1.6 行守卫断言该操作名与表里的路径一致，避免下次再错位 |

## 建议（不阻断合并）

- **S-1 措辞层面把「内联体上限」与「合成能力」分开。** 现在的 `RenderUnitText.description` 写「one speech synthesis is bounded at `MAX_SPEECH_TEXT_CHARS` (4096)」，把**一个 Job 的内联请求体上限**说成了**合成能力上界**。当前为真（`tts` + `text_asset_id` 在生产准入被 `job_text_asset_unsupported` 拒绝，长文通道未开放），但 #696 一落地该句即失效。建议改成机制式表述：「派发把单元正文内联进一个公开 Job 请求体，内联正文上界是 4096」。
- **S-2 给对照表的 `param` 与「声明上限」两列补断言**（G1 证明两列现在无守卫，各 1 行 `_rows_naming` 即可）。
- **S-3 把第 7 处守卫改成规则式**：断言「凡 schema 名匹配 `RenderUnit*` 且 `properties` 含 `text` 的，其 `text` 必须是该 `$ref`」，替代当前写死的 6 个名字（G4 证明新增同名族字段不会被发现）。
- **S-4 在仓库内补一条端到端派发用例**：本 PR 的 HTTP 层用例用假 service 抛 `RenderSubmissionRejectedError`，真适配器的拒绝只由既有 `test_public_speech_render_jobs.py` 覆盖，两者之间没有整体链路的仓库测试。我手工跑通的装配方式可复用：`SPEECHNEXUS_WORKBENCH_SPEECH_BASE_URL` 指向一个进程内起的公开语音 ASGI 应用（同时必须配上完整对象区配置，`runtime_settings.py:244-254` 把语音 origin 与对象区绑定），断言 4097 派发 422 且语音侧 0 请求。
- **S-5 issue #704 的收尾口径**：B 是对「走同一路径 → A」二分规则的重解释，建议在 issue #704 的收尾说明里写明「实测命中 A 的条件，但 A 的收益仅为提前拒绝、代价是跨层下沉与迁移」，由 issue owner 确认 AC 判定。
- **S-6（本 PR 外）** 200000 缺少来源说明（为什么是 200000 而非 50000/1000000），且工作台编辑器不提示 4096 派发上界。建议开卡：编辑器提示 + 派发错误文案，或并入 #696。

## 独立复现的证据（环境与命令）

环境：macOS arm64；系统 node `v26.12.1`；`uv 0.13.5`；Python 3.14；`pnpm 12.14.0`。被审 head 在 detached 快照 `/private/tmp/sn740-pr` 检出（`uv sync --frozen --all-packages`），审查分支 `/private/tmp/sn740` 只放本报告。

隔离 PG（`disposable`，只走 unix socket，端口仅本机）：`initdb` + `pg_ctl -o "-k <sock> -p 55441 -c listen_addresses=''"`；按 `tests/integration/test_workbench_runtime.py:70` 的守卫要求建 `speechnexus_workbench_test` 库、`workbench_test_owner` 角色与 `speechnexus.disposable_workbench_probe=on`。用完 `pg_ctl stop` 并删除数据目录（无残留监听）。

完整测试与检查（全部在 head 上实跑）：

| 项 | 结果 |
| --- | --- |
| `uv run pytest tests/contract -q` | **3747 passed**（2:35） |
| `uv run pytest tests/unit/backend -q` | **3051 passed**（0:41） |
| `uv run pytest tests/integration/test_workbench_render_units_http.py tests/integration/test_workbench_render_units_postgres.py -q`（隔离 PG） | **12 passed, 1 error**（error = 缺隔离 MinIO，见未验证项） |
| `uv run pytest tests/unit/test_operation_coverage.py -q` | **15 passed** |
| `cd web/workbench && pnpm test` | **873 passed / 62 files**（3.34s） |
| `uv run ruff format --check .` | 1841 files already formatted |
| `uv run ruff check .` | All checks passed |
| `uv run mypy --platform linux` | Success: no issues found in 1450 source files |
| `uv run lint-imports` | 9 kept, 0 broken |
| 契约六项检查（第 9、10 条） | 全部 passed（见该节输出） |

## 新绿

本 PR 新增 8 条测试（契约 7 + 单元 1），我逐条确认在 head 上通过、在去掉 PR 内容后变红：

- **去掉 PR 的契约与文档内容**（把 `contracts/workbench-openapi.yaml`、`contracts/speech-openapi.yaml`、`docs/architecture/speech-api.md` 换回 base 版本）→ **6 failed, 7 passed**：`test_render_unit_text_bound.py` 4 例全红，`test_tts_text_bound_consistency.py` 的 `test_both_entries_document_one_limit_and_their_two_codes`、`test_the_cross_entry_bound_table_states_each_entry_code` 红。说明新增内容是真实守卫而非同义重述。
  - 8 条里唯一在 base 上也绿的是 `test_the_same_over_bound_body_keeps_each_entry_point_code`：它是**行为钉**（把 #705 的决定固定在当前行为上），M6 证明它能挡住「统一码」方向的改动。
- 作者报的「基线 3740 / 3050」我未直接跑基线条数，但核对为算术一致：diff 中新增 4 + 3 个契约测试函数、1 个单元测试函数，7/1 与 3747−3740、3051−3050 吻合。

## 只能依赖作者陈述 / 未验证项

- **缺隔离 MinIO**：`test_workbench_render_units_postgres.py::test_a_unit_reports_only_its_own_projects_retained_copy` 报 `isolated MinIO service is required for object evidence`（与作者 PR 正文的「缺 MinIO，1 error」一致）。该例覆盖 saved-copy 保留，与本 PR 改动无交集；未跑。
- **缺 MySQL**：`tests/integration/test_public_job_admission_http_pg.py` 6 例 `Failed: Production Job behavior requires both isolated PG and InnoDB DSNs; missing SPEECHNEXUS_TEST_MYSQL_URL`。该套件里 `test_text_above_the_plan_ceiling_is_refused_without_rows` 是 #690 时代对「4096 允许 / 4097 拒绝且不写行」的双库强证据，本次未执行，只做了代码阅读（`PLAN_TEXT_CEILING` 与 202/422 断言）；按仓库规则缺配置即未验证，不用 skip 冒充。
- **对照表两行（文本资产 1000000 字符 / 上传 4 MiB）**：仅做契约 + 代码（`settings.py:57`、`domain/assets.py:55`、`asset_errors.py:19-20`）核对，未实跑——资产上传需要隔离 MinIO 运行态。
- **真实引擎/真实浏览器端到端未跑**：本 PR 无运行时改动，我没有启动引擎或浏览器；语音底座在探针里是真实 ASGI 应用 + 假准入/假合成，用于观测「请求是否到达」而非音频产物。
- 其余作者陈述（单实现、0 请求、6 处、检查结果、例数）我都独立复现，未发现不一致；作者的反例数值与我不同的部分仅因运行范围不同（单文件 vs 4 文件）。

## 时间线（过程记录）

| 时间（Asia/Shanghai） | 动作 |
| --- | --- |
| 13:10 | 读主工作区状态（`uv.lock` 已有他人改动，不动）；`git fetch`；从 `origin/main`=`d58127c0` 拉 `review/740-text-limit` 到 `/private/tmp/sn740` |
| 13:11 | 建 detached 快照 `/private/tmp/sn740-pr`（`ab92c3ee`），`uv sync --frozen --all-packages` |
| 13:12 | 读被审 diff（15 文件）：两契约、`speech-api.md`、3 个测试、7 个生成物、audit 摘要 |
| 13:14 | A1 调用链 grep：单实现 / 单调用点 / 单装配点 / 唯一出站 POST；确认 v1 派发未注册 |
| 13:15 | A2 口径一探针（真领域 + 真适配器 + MockTransport）：4096→1 请求、4097/20000/200000→0 请求 |
| 13:16 | B4 八点探针：同步四类全 400 / 异步四类全 422；补测同步既有 422 轴（`text_commit_mode=sentence`、`Idempotency-Key`） |
| 13:17 | 起契约/单测两个后台任务；`pnpm test` 873/62；`ruff`/`lint-imports`/`mypy` 全绿 |
| 13:18 | 契约项检查：drift / validate / route-conformance / speech-changes / http-changes / compat 全过；operation-coverage 必要性与 diff 范围核实 |
| 13:20 | 建 disposable PG 集群，按守卫要求建库建角色；render-units 集成两套 → 12 passed / 1 error（MinIO） |
| 13:22 | 变异单点计量 M1~M6（每个 1 hunk、单独跑、单独还原），并复现作者两条 #705 反例 |
| 13:24 | 缺口探针 G1（表列全绿）、G4（第 7 处全绿）；去掉 PR 内容验证 6 红 |
| 13:26 | HTTP 端到端探针：200000 存 201 / 派发 422 + 语音侧 0 请求 + generations 空 / 改回 4096 恢复派发 |
| 13:34 | 停 PG、删数据目录；写本报告；`ruff format --check .` 复核；提交并开审查 PR |
