---
title: PR #939 非作者审查：/v1/models readiness 静态投影的定性与守卫
status: active
---

# PR #939（`fix/catalogue-readiness-projection`）readiness 静态投影定性的非作者审查

**不能按现状合，但只差一句**：四条主张里三条（workspace 口径、授权边界硬地基、占位路由与
`#863`）实测成立，写集干净、契约未动、作者的两条反例逐项复现；阻断项是一句**事实错误**——
runbook 说契约对该取值的定义「要目录真的接入实时事实才成立」（`docs/operations/async-job-runbook.md:483`，
被审 head 口径），而准入的 workspace 维度不可能由这个端点表达，接入再多实时事实也满足不了契约里
「currently admissible / cannot be admitted now」（`contracts/speech-openapi.yaml:4063-4070`）；
顺带这台装配**结构上**永不能承诺该取值。把契约缺陷记成「等接入」，正是这个缺陷悄悄发布的方式。
改这一句（并钉住契约冲突）后可合，其余是建议级。

被审对象：PR #939《docs: 目录 readiness 是静态投影，并加守卫钉住这个说法 (#876 D12)》，
head `6afa7aef`。分叉点 = `origin/main~1` = `1721ddb2`（`git merge-base origin/main 6afa7aef`）；
`6afa7aef` 不是 `origin/main` 的祖先（`origin/main` = `b1b8912b`，根 `54f7778e` 之后的线性历史）。
按**分叉点口径** `git diff 1721ddb2 6afa7aef` = **4 文件 +67/−3**：
`backend/src/speechnexus_backend/model_catalog_runtime.py`(+12，纯注释)、
`docs/operations/async-job-runbook.md`(+12/−3)、`scripts/dev-env/check-doc-statements.py`(+24/−1)、
`tests/ci/test_doc_statements.py`(+22/−0)。本报告未使用两点 `git diff origin/main 6afa7aef`
（本次两者数值恰好相同，口径仍按分叉点）。

本报告所有 `path:line` 行号按**被审 head `6afa7aef`** 口径给出（用 `git show 6afa7aef:<path>` 核）。

审查方式：`/private/tmp/sn944` 是从 `origin/main`（`b1b8912b`）新建的审查分支
`review/939-catalogue-readiness`，**只放本报告**；被审 head 用 `git checkout 6afa7aef` 读取，
构建、探针与变异实验在该树上做，报告写回审查分支后再核 `git status --porcelain`。
`check-review-pr-boundary.py` 要求本分支只承载 `docs/reviews/**`，本报告是该分支与 `origin/main`
的唯一差异。

环境：macOS arm64，PostgreSQL 16.16（`sn892`，`SPEECHNEXUS_TEST_DATABASE_URL`），
`UV_NO_CONFIG=1 uv sync --locked --all-packages --all-groups` 成功，Python 3.14。

## 0. 结论与必修清单

| 编号 | 级别 | 事实 |
| --- | --- | --- |
| P1 | 必修 | `docs/operations/async-job-runbook.md:483`（被审 head）把契约口径的落空写成「要目录真的接入实时事实才成立」。**这句不成立**：`admission_state` 的取值定义是 per-workspace 的（准入按 `job.workspace_id` 计费、按 `speech_assets.workspace_id` 过滤素材，见 `public_job_admission.py:695`/`:514`），而 `/v1/models` 的签名没有 workspace（`backend/src/speechnexus_backend/speech/interfaces/http/models.py:42`），契约 `:55-63` 只有 `X-Request-ID`/`If-None-Match`。接入实时事实最多让 `engine_ready`/`quality_qualified` 变真，`admission_state` 的两个准入取值仍然无法在此端点表达。正确处置是把它登记为**契约缺陷**（改口径 / 加 `workspace_id` / 把消费者指向 `admission-readiness`），不是记为「待接入」。 |
| P1 | 必修 | 同一处没有指出契约与实现的**具体差异**，而 `docs/agent-guide.md:44` 明确要求「冲突须指出具体差异，不通过放宽校验掩盖」。至少点名 `contracts/speech-openapi.yaml:53-54`（`current admission facts`）与 `:4063-4070`（`available means currently admissible` / `cannot be admitted now`），并给出跟踪项（issue）。现状把「契约措辞偏强但不是行为承诺」当结论，而仓库里**没有任何文档**给 PRD 高于契约的排序：`docs/agent-guide.md:13` 把 `contracts/` 列为「协议…的权威来源」，`:44` 说「**机器契约定义协议预期**」。 |
| P2 | 建议 | 守卫「钉住这个说法」名不副实：三条 `required` 都是子串，`forbidden` 只是两句裸子串，`anchor` 只钉注释首行（`check-doc-statements.py:198-207`）。实测**语义反转仍全绿**（§4 的 M5/M7/M8）：保留三句原文再加一句「实测：实例 `ready` 后会报 `available`」、或把注释实质内容删到只剩首行，守卫都 `RESULT OK`。 |
| P2 | 建议 | `forbidden` 裸子串会误伤否定式引用：往 runbook 写「本端点不是反映实时状态的通道」即红（§4 的 M4）。同一文档将来为说明历史而引用旧说法，会被这条挡下。 |
| P3 | 可选 | 守卫的 `anchor` 与行号强耦合：把注释整体下移一行（语义不变）即红（§4 的 M3），任何插入到 `model_catalog_runtime.py:92` 之前的改动都要连带改 runbook。这是既有 12 条 claim 的同构设计，不是本 PR 新引入，但本 PR 把「注释第 92 行」变成了硬约束。 |
| P3 | 可选 | `runbook:479` 把 `:92` 说成「构造目录时不传 readiness 事实」，实际构造在 `:104-106`（`PostgresModelCatalog(pool, timeout_seconds=…)`，无 `readiness=`），`:92` 只是注释首行（`anchor` 要求它）。措辞建议改成「见 `:92` 起的装配注释」。 |
| P3 | 可选 | 启动拒绝的**可发现性**弱：实测越界授权时 `open()` 抛的是 `ValueError: Model catalogue reader startup rejected; verify migration and reader role`，具体原因被 `from None` 抹掉（`model_catalog_runtime.py:110-122`），运维看不到「读越界」这句（§2）。注释说「rejected at startup」为真，但别指望运维能据此定位。 |
| P3 | 可选 | 溯源：注释与守卫注释都写 `#876 D12`，但 `docs/reviews/` 下没有 `#876` 的审查文档带 D12 标签（D12 在其它报告里含义各不相同）。D12 原文只能从 issue 取，仓内不可自证。 |

作者四条主张的判定：**1 成立（需补一处限定）、2 成立（实测，见 §2）、3 不成立（见 §3）、4 成立**。
技术主体成立：`/v1/models` 的 readiness 确实是 fail-closed 静态投影、四个取值恒定、且不随实例
注册/`ready`/Job `202` 变化（§1、§5），这点比改动前的「`admission_state` 在未注册实例时是
`unavailable`」（`origin/main` 的 `docs/operations/async-job-runbook.md:481`）准确得多。

## 1. 主张 1（准入按 workspace、`/v1/models` 无 workspace）逐条核对

| 作者主张 | 判定 | 依据（被审 head） |
| --- | --- | --- |
| `public_job_admission.py:695` 起用 `job.workspace_id` | **成立** | `quota = _quota(cursor, job.workspace_id)` 在 `:695`；`:734` 音频同样传 `job.workspace_id`，`:750` 落 `workspace_id` |
| `_quota()` `:177` join `workspace_quotas`+`speech_workspaces` | **成立** | `:177` 定义；`:194-197` `FROM workspace_quotas AS q JOIN speech_workspaces AS w … WHERE q.workspace_id=%s` |
| `_audio()` `:514` 按 `speech_assets.workspace_id` 过滤 | **成立** | `:514` 定义；`:525` `WHERE workspace_id=%s AND asset_id=%s` |
| `_instance()` `:211` 只取 `instance_state='ready'` | **成立，且它不是 workspace 维度的** | `:222` `WHERE e.binding_revision_id=%s AND e.instance_state='ready'`。实例选择只按 binding/model，跨 workspace 共用——这正是「静态按模型投影」与「按 workspace 准入」不能互相还原的根源 |
| `/v1/models` 签名无 workspace | **成立** | `backend/src/speechnexus_backend/speech/interfaces/http/models.py:42` `async def list_models(request: Request) -> Response` |
| 契约 `:55-63` 只有 `X-Request-ID`/`If-None-Match` | **成立** | `contracts/speech-openapi.yaml:56-63`，无 workspace 参数 |
| 带 workspace 的是 `/v1/nexus/capabilities`（`:305-316`，实现 `backend/src/speechnexus_backend/speech/interfaces/http/models.py:89`） | **成立，但需限定** | `backend/src/speechnexus_backend/speech/interfaces/http/models.py:89` `get_capabilities(request, workspace_id: str | None = None)`；`capability_snapshot.py:132`/`:142` 只把 `workspace_id` 放进快照身份，**不据此过滤目录**——`docs/prd.md:1011` 原文即「当前能力接口仅校验 workspace 标识格式，不验证其存在或据此过滤目录」。所以「有 workspace 参数的目录端点」存在，但今天没有任何端点按 workspace 投影准入 |

推论（比作者更进一步）：**接不接实时事实不是关键，workspace 维度才是**。目录是「按模型」的，
准入是「按 workspace 计费 + 全局选实例」的；`/v1/models` 缺 workspace 参数，因此即使把
`engine_instances`/`workspace_quotas`/`speech_assets` 全部接进来，它对 `admission_state` 也只能
给一个跨 workspace 的、必然对某些调用者为假的答案。这一点决定了 §3 的处置方向。

## 2. 主张 2 的实测：授权边界是不是硬地基

代码上是：`READABLE`（`model_catalog_runtime.py:35-43`）只有
`speech_publications`、`speech_published_binding_revisions`、`catalog STATE_TABLE`、
`RETIREMENT_TABLE`、`HEAD_TABLE`，加 voice 视图 `VOICE_READER_VIEW`；`_verify_role`
（`:155`）在 `:207-210` 对**任何**越界 SELECT 抛
`ValueError("Model catalogue reader reads beyond its published surface")`。声明不够，我按真实库做了一次。

**做法**（`/private/tmp/rev939_probe.py`，可复算）：在 `sn892` 建一次性角色 `rev939_reader`，
授予 `speech` schema 的 USAGE + READABLE 五表 + voice 视图的 SELECT（即生产
`model_catalog_reader_grants`/`voice_catalogue_reader_grants` 的形状），用
`Settings(process_role="api", model_catalog_reader_database_url=<该角色>)` 调
`ModelCatalogRuntime.open()`；然后只加一条 `GRANT SELECT ON speech.engine_instances` 再 open；
同时用该角色真实连接直接调 `_verify_read_only()` 读守卫自己的消息。跑完 `DROP OWNED BY` + `DROP ROLE`。

| 步骤 | 结果 |
| --- | --- |
| 基线（恰好等于已发布表面） | `open() SUCCEEDED; catalogue=PostgresModelCatalog` |
| 加 `GRANT SELECT ON speech.engine_instances` 后 `open()` | **失败**：`ValueError: Model catalogue reader startup rejected; verify migration and reader role` |
| 同一次越界，直接调 `_verify_read_only()` | **失败**：`ValueError: Model catalogue reader reads beyond its published surface` |
| 撤销该授权后 `open()` | 重新成功 |
| 异常链 | `__cause__=None`、`__suppress_context__=True`（`from None` 在 `:110-122`） |

**结论：主张 2 成立，且不是注释里的说法。** 越界授权在启动即被拒，代码路径与消息逐字一致。
两条限定：(a) 运维实际看到的只有通用文案，`reads beyond its published surface` 被 `from None`
抹掉（P3）；(b) 这条守卫守的是**授权**，不是**意图**——作者说「依据是授权边界而非意图」在
方向上对，但把配置事实说成不可动的硬约束要小心：放宽 `READABLE` 并同时授权，守卫会跟着放宽，
真正拦住「接实时事实」的是 workspace 维度（§1）与契约口径（§3），不是 RBAC 本身。

补充反证（代码口径）：越界不限于表——`READABLE|{VOICE_READER_VIEW}` 之外任何 schema 的 SELECT
（`:207-209` 的 `schema_name != schema`）、列的读写（`:205-206`）、序列（`:217-229`）、
SECURITY DEFINER 函数（`:231-239`）全部在启动拒绝。所以「用视图或 definer 函数绕开」这条路也堵着。
`tests/unit/backend/test_model_catalog_runtime.py:351-379` 已用 fake 覆盖列的越界读。

## 3. 关键判断：契约是不是行为承诺（本节定方向）

### 3.1 契约原文（被审 head 未改动）

- `contracts/speech-openapi.yaml:52-54`：`GET /v1/models` 的 `description`——「The `nexus` object on
  each model separates binding revision, support, quality qualification, and **current admission
  facts**.」
- `:4063-4070`：`ModelReadiness.admission_state` 的 `description`——「**available means currently
  admissible**; queueable means …; capacity_full means a required reservation is currently
  unavailable; **unavailable means the model cannot be admitted now**.」
- `:1215`：实时面被单独辟给 `GET /v1/nexus/jobs/admission-readiness`，其 `description` 明写
  「Report whether a public Job create would be admitted **right now**」，并解释与 `/readyz` 的分工；
  该路由带 `WorkspaceIdQuery`（`:1302`），当前在 `unimplemented.py:89` 的
  `UNIMPLEMENTED_OPERATIONS` 里答 503，跟踪 `#863`（实测存在：`#863` = 「准入可观察面 B：选择层与
  准入层收敛成一个函数，报告走同一路径」）。

### 3.2 权威排序：仓库没有「PRD 高于契约」

| 证据 | 原文 |
| --- | --- |
| `docs/agent-guide.md:13` | 「`contracts/`、有效 ADR、架构文档｜协议、设计和长期决定的**权威来源**」 |
| `docs/agent-guide.md:44` | 「当前代码与可复现运行结果说明已实现行为；**机器契约定义协议预期**；…冲突须指出具体差异，不通过放宽校验掩盖。」 |
| `contracts/AGENTS.md` | 「OpenAI 兼容范围限于声明的子集；扩展使用 `nexus` 命名空间，**不改变标准字段含义**」「schema 语法通过不能单独证明语义冻结或路由可用」 |
| `docs/prd.md:1011`（M-CATALOG 验收） | 「缺少相应就绪或资格证据时保守投影为 `false`，无法证明可准入时 `admission_state` 为 `unavailable`…**默认目录也未接入实时 readiness 证据，不能用响应证明该工作空间已获准入**。」 |

结论：**契约的描述文本在本仓库的权威模型下构成协议预期，不是「措辞偏强」**。OpenAPI 里
`description` 不被 schema 校验器强制，但 `docs/agent-guide.md:44` 正是把「协议预期」放在机器契约上；
客户端和生成器读的就是这段文本。同时 **PRD 那一句不是「PRD 优先」的证据**，它是同一缺陷的**第二次
披露**（「不能用响应证明该工作空间已获准入」），并没有说「契约描述可以不算数」。所以不存在
「PRD 把静态投影定为验收条款 → 契约措辞作废」这条推理；正确读法是：**PRD 承认目录没有实时证据、
并禁止用响应证明准入；契约却把该字段描述成实时准入事实——两份权威文档冲突**，按
`agent-guide.md:44` 必须点名差异。

### 3.3 于是「缺陷」的方向是什么

不是「实现缺实时事实」，而是「**契约在这个端点上给了一个不可能满足的定义**」：

1. `admission_state` 的 `available`/`unavailable` 是 per-workspace 判据（§1）；
2. `GET /v1/models` 没有 workspace 参数（`backend/src/speechnexus_backend/speech/interfaces/http/models.py:42`，契约 `:56-63` 的参数表只有
   `X-Request-ID`/`If-None-Match`）；
3. 实例就绪是全局的、配额与素材是 workspace 的（`_instance` `:211` 不按 workspace 过滤；
   `_quota` `:177`、`_audio` `:514` 按 workspace）——两类事实不可能压进一个无 workspace 的字段。

因此处置只有三条，都属契约层：把该字段的口径改成「目录级静态就绪事实」（改描述/枚举语义）、
给 `/v1/models` 加 workspace 维度、或把消费者引向 `GET /v1/nexus/jobs/admission-readiness`（`:1215`）。
`runbook:483` 的「接入实时事实就成立」不在其中，它把契约缺陷降级成实现进度，读者（尤其按契约
生成的客户端）会继续把 `unavailable` 读成「现在不能准入」。

## 4. 守卫质量：误红与漏红（8 例变异，全部在本报告作者手里复现）

做法：只把 `check-doc-statements.py` 读取的 17 个文件复制到临时树（与
`tests/ci/test_doc_statements.py` 同法），在副本上施加变异后以 `--root` 跑守卫；**被审 worktree
未改动**。基线（未变异）`RESULT OK`，`tests/ci/test_doc_statements.py` 11 passed。

| # | 变异 | 期望 | 实测 |
| --- | --- | --- | --- |
| 基线 | 无 | 绿 | `RESULT OK` |
| M1（作者） | 删注释首行 | 红 | **红**：`cites …:92 but none of those lines contains 'Static, fail-closed projection'` |
| M2（作者） | runbook 退回「反映实时状态」 | 红 | **红**：`no longer says '是**静态投影**（fail-closed），不是实时状态'` + `still says '反映实时状态'`（2 条） |
| M3（本报告加） | 注释整体下移一行（语义不变，`anchor` 指向 +1） | 红？ | **红**：runbook 仍引 `:92`，该行已不是注释首行 → 行号耦合导致的红（P3，不是语义误报） |
| M4 | 合法否定句含 forbidden 子串（「本端点不是反映实时状态的通道」） | — | **红**：`still says '反映实时状态'` → **误伤**（P2） |
| M5 | 保留三句 `required` 原文，追加「实测：实例 `ready` 后本端点会上报 `available`」 | 应红 | **绿** → **漏红**（P2，最严重） |
| M6 | 删掉注释的全部实质论证（READABLE 名单、无 workspace 的说明），只留首行 | 应红 | **绿** → **漏红**（P2） |
| M7 | 把整段注释改成一行「Readiness is not wired yet.」，只留首行 | 应红 | **绿** → **漏红**（P2） |
| M8 | 在同一文档别处加矛盾句「实例变成 `ready` 后该字段会跟着变」 | 应红 | **绿** → **漏红**（P2） |

判读：

- **M1/M2 复现作者的两条反例，且方向正确**——但它们证明的是「首行/三个子串被改动会红」，
  不是「这个说法被钉住」。M5/M7/M8 说明**同一段文字可以在语义上被整体反转而全绿**，M6/M7 说明
  注释里唯一承载「为什么静态」的论证（READABLE 名单、无 workspace）**完全没有被钉**，只钉了首行。
- **M3 是行号耦合**：`_check_anchor`（`check-doc-statements.py:101-115`）要求文档引用的
  `path:line` 里有一行**含有** marker，任一命中即过。所以注释下移一行会红，但这不是「说法变了」，
  而是「引用过期了」。既有 12 条 claim 同构，可接受；代价是本 PR 之后，任何人往
  `model_catalog_runtime.py:92` 之前插一行，CI 就红并要求同步改 runbook。
- **M4 是子串匹配的固有误伤**：`forbidden` 是 `phrase in text`（`:92-96`），不区分否定语境。
  若担心误伤，可把 forbidden 收紧到原句（如「在未注册实例时是 `unavailable`」整句）并把
  「反映实时状态」改成与否定语境的共存检查。
- 建议（不阻断）：把注释的实质句（`READABLE` 语义 + 「no workspace」）写进 `required`，
  或把 `anchor` 改钉 `READABLE = frozenset(` 并把 runbook 的 `:92` 改成 `:35`（`READABLE` 定义行），
  这样 M6/M7 类删改至少会在“注释被改”上留下证据。

## 5. 披露是否诚实：runbook 段落的实测核对

先核事实（全部成立）：`/v1/models` 的 `readiness` 四项恒定——`configured` 是硬编码 `True`
（`speech/application/model_catalog.py:108`），其余三项来自 `ReadinessFacts()` 默认值
（`:55-60`：`engine_ready=False`、`quality_qualified=False`、`admission_state="unavailable"`；
`:96` `measured = readiness or ReadinessFacts()`），而装配处的
`PostgresModelCatalog(pool, timeout_seconds=…)`（`model_catalog_runtime.py:104-106`）不传 readiness，
`model_catalog_postgres.py:35` 同样回落默认值。所以「不随实例注册、实例 `ready` 或 Job `202` 变化」
在代码上为真，`unavailable 只表示没有建立这项事实` 也是这套实现的真实读法。

**披露 vs 淡化**：这段是**披露，且方向正确**——它第一次把「为什么恒为 `unavailable`」和
「别拿它判准入」写进了运维文档，比 `origin/main:481` 的旧说法（「在未注册实例时是 `unavailable`」，
暗示它会随实例注册变化）诚实。**但结尾一句是淡化**：`:483` 把契约口径的落空解释为「等目录接入
实时事实」，读者会得到「以后接上就对上了」的印象；而按 §3.3，接上也不对上（workspace 维度缺失）。
所以读者读完**仍可能**以为该端点将来能判可准入、或以为 `unavailable` 是「暂时没有证据」——
后者正是契约禁止的读法。修法见 §0 的 P1：把「待接入」换成「契约口径与本端点结构性不符」，
并点名 `:53-54`/`:4063-4070`（改口径 or 加 workspace or 引向 `:1215`）。

另：`:484-486` 指出的实时面（`POST /v1/nexus/jobs` 的实际应答码 + 契约声明的
`GET /v1/nexus/jobs/admission-readiness`）**准确**——`unimplemented.py:89` 确实把该路由登记为占位，
`#863` 确实是它的实现跟踪项，runbook 里的「第 11 节」也确实存在
（`## 11. 第 7 步：提交并观察一次 Job`）。

## 6. 写集核对

`git diff --name-status 1721ddb2 6afa7aef` 只有 4 项，与 PR 自述一致：

| 文件 | 性质 |
| --- | --- |
| `backend/src/speechnexus_backend/model_catalog_runtime.py` | **+12 纯注释**（`:92-103`），`open()` 的执行顺序、异常、池参数未变；`git diff -w` 后只剩注释块 |
| `docs/operations/async-job-runbook.md` | D12 段 +12/−3，只动 `/v1/models` 那一小节 |
| `scripts/dev-env/check-doc-statements.py` | 新增 `RUNBOOK`/`CATALOG_RUNTIME` 常量、`STALE_LIVE_READINESS_PHRASES`、第 13 条 `Claim`；`CLAIMS` 由 12 → 13（实测输出 `13 capability statements`） |
| `tests/ci/test_doc_statements.py` | 新增 2 条用例（`:157` 文档侧、`:166` 代码侧），`COPIED_FILES` 加 `RUNBOOK`/`CATALOG_RUNTIME` |

**契约未动**：`contracts/` 零差异（`git diff 1721ddb2 6afa7aef -- contracts/` 为空），
`packages/contracts/` 生成物零差异，前端/SSR 消费者零差异。作者说「按硬约束没改契约」属实。
守卫在 CI 里是**间接**生效的：`scripts/dev-env/check-doc-statements.py` 没有单独的 workflow 步骤，
但 `Guards` 作业跑 `python3 -m unittest discover -s tests/ci`（`.github/workflows/ci.yml:102`），
其中 `test_the_real_checkout_passes` 会以 `ROOT` 为根跑守卫，因此新加的两条用例与真实树都会进 CI。

## 7. 反例重做与变异还原

作者的两条反例逐项复现（§4 M1/M2），我另加 6 条（M3–M8）：`anchor` 行号 +1 **会红**（行号耦合，
非语义误报）、否定式引用**会误红**、三处语义反转**全绿**。所有变异在临时副本上施加，
被审 worktree 未改动；报告提交前 `git status --porcelain` 为空（见 §8）。

## 8. 复核记录

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 分叉点口径 | `git merge-base origin/main 6afa7aef` / `git diff --stat 1721ddb2 6afa7aef` | `1721ddb2`；4 文件 +67/−3 |
| 祖先关系 | `git merge-base --is-ancestor 6afa7aef origin/main` | 非祖先（未合入） |
| 守卫基线 | `python3 scripts/dev-env/check-doc-statements.py` | `RESULT OK`（6 路由、13 claims） |
| 守卫用例 | `uv run pytest tests/ci/test_doc_statements.py -q` | 11 passed |
| RBAC 实测 | `/private/tmp/rev939_probe.py`（真实 PG `sn892`，一次性角色已删） | 基线成功 → 越界授权启动失败 → 撤销后恢复（§2） |
| 变异实验 | `/private/tmp/rev939_mutate.py`（临时副本，`--root`） | 8 例，见 §4 |
| 契约差异 | `git diff 1721ddb2 6afa7aef -- contracts/ packages/contracts/` | 空 |
| 篡改还原 | `git status --porcelain` | 空 |

## 9. 交给作者的动作

1. **P1（必修）** 改 `runbook:483`：把「契约定义要目录真的接入实时事实才成立」换成
   「契约把 `admission_state` 定义为实时取值（`contracts/speech-openapi.yaml:4063-4070`），本端点
   无 workspace 维度（`:55-63`、`backend/src/speechnexus_backend/speech/interfaces/http/models.py:42`）而准入按 workspace 计（`public_job_admission.py:695`），
   故该契约口径与本装配结构性不符，按契约缺陷跟踪」；并点明 `:53-54` 的「current admission facts」。
2. **P1（必修）** 给出跟踪项（issue 号写进 runbook 或 PR 正文），否则 `docs/agent-guide.md:44`
   的「冲突须指出具体差异」没有落点。
3. P2：要么把注释实质句纳入 `required`（或 `anchor` 改钉 `READABLE` 定义行），要么在 runbook/PR
   里说明守卫只覆盖首行与三个子串——现在「加守卫钉住这个说法」的说法比守卫的实际能力大（§4）。
4. P3：`runbook:479` 的 `:92` 措辞（注释首行 ≠ 构造点 `:104-106`）、启动拒绝消息被 `from None`
   抹平这两条，建议顺手写进注释，省下一次误判。
