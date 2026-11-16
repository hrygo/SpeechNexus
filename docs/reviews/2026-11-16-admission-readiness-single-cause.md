---
title: "PR #870 非作者审查：准入报告单值化与契约前置（#862）"
status: active
version: "0.1.0"
date: 2026-11-16
---

# PR #870 非作者审查：准入报告单值化与契约前置（#862）

审查对象：PR [#870](https://github.com/hrygo/SpeechNexus/pull/870) `feat/862-single-cause`，
head `bd9d7d0e`（`feat: 准入报告的 cause 收成单值枚举`），基线 `origin/main` = `c51c5443`。
关联：#862（单值化）、#845（准入可观察面）、#863（报告实现）、#473（占位路由机制）。

本报告只审 PR #870 自身：审查分支 `review/870-862` 基于 `origin/main` = `c51c5443`，
本报告是该分支与 `origin/main` 的唯一差异。

## 0. 结论

**能合**：产品代码与契约没有阻断性缺陷——改动的路由顺序在整张路由表上只有一个
意图内的竞争对、闭包双向可证伪、生成物零漂移。但当前 head **不建议直接合**，因为它
把两处事实错误写进了仓库：测试注释里的契约论证是错的（并且掩盖了 #863 的真实约束），
审计文档 §6.2 的计数自相矛盾；另有一处装配注释把守卫的能力说大了。三条都是一两行的
返修，不需要重新设计。

| 编号 | 级别 | 事实 |
| --- | --- | --- |
| P1 | 必修 | `tests/contract/test_job_admission_readiness.py:341-345` 声称「字面段不可能是一个 Job identifier（`Identifier` 排除 `-`），所以两条路由不会混淆」。`Identifier` 的 pattern 明确允许 `-`，真实 job id 形如 `job-<hex>`。占位移除后唯一决定胜负的是注册顺序；实测 `{job_id}` 先注册时字面路径被 `getJob` 接管。 |
| P1 | 必修 | `docs/operations/contract-conformance-audit.md:381-384` 新写的「10 条」与紧邻的「6 条指向 `OperationNotImplemented` …三条沿用 `ServiceUnavailable`」（合计 9）矛盾；实测 6 + 4 = 10。同一文件 `:424` 仍写「当前 9 条占位中 6 条…3 条」，`:356`（§6.1）又声明「条数以 `KNOWN_UNIMPLEMENTED_OPERATIONS` 为准，本文不复制」。 |
| P2 | 建议 | `backend/src/speechnexus_backend/app.py:727-734` 的装配注释把「顺序不会让占位吞并已交付操作」说成已由守卫守住。守卫只覆盖同一 `method+template`（顺序无关）；跨 template 的具体路径重叠（占位 wildcard 抢真实 literal）它看不见——本报告第 2.5 节给了可复现反例。当前仓库里唯一的重叠正是本 PR 意图，所以无实际缺陷，但注释把守卫的能力说大了。 |
| P3 | 可选 | 新路由的 503 用共享 `ServiceUnavailable`（任意 `error.code` 都通过校验），因此操作描述里「Job 面未装配时返回 `job_unavailable`」在占位期不可观测：两种装配态实测都返回 `operation_not_implemented`。建议在描述里显式写出占位期的回答，或接受审计文档已给出的解释。 |

## 1. 复现口径

```bash
git worktree add /private/tmp/sn873 -b review/870-862 origin/main
cd /private/tmp/sn873 && git checkout bd9d7d0e
UV_NO_CONFIG=1 uv sync --locked --all-packages --all-groups   # 与 CI 的 uv-sync-retry.sh 同参
pnpm install --frozen-lockfile                                 # TS 契约消费者用例需要
```

`~/.config/uv/uv.toml` 指向清华镜像，会让 `uv sync --locked` 报 lockfile 过期、并让
`make` 内的 `uv run` 把 `uv.lock` 的所有 `source` 改写成镜像 URL。这是本机配置问题，
**不是仓库缺陷**；全程用 `UV_NO_CONFIG=1` 绕开，未写入失败项。

所有「已实现路由」「契约文本」的事实取自 HEAD 工作区或 `git show origin/main:<path>`；
主检出停在 `docs/tracker-batch12`，未用于取证。

## 2. 路由挂载顺序：副作用审查

### 2.1 改了什么

`create_unimplemented_router()` 从 `create_app()` 的末尾（`origin/main` 的
`app.py:792`，`create_quarantine_release_router` 之后）移到中间件装配之后、
全部 feature router 之前（`app.py:735`）。动机是新增的
`GET /v1/nexus/jobs/admission-readiness` 与 `GET /v1/nexus/jobs/{job_id}` 同深度，
后者先注册就会接住字面路径。

### 2.2 整张路由表的竞争关系（实测）

以真实 `create_app()`（注入内存 Job 服务）组装，枚举 `app.router.routes`：
**72 条路由 = 62 条真实路由 + 10 条占位路由**，真实路由的 `(method, 归一化 template)`
无重复。按 Starlette 的整路径匹配，两条路由只有在**段数相同**时才可能竞争；同深度下
逐段比较，占位↔真实之间**只有一个** (literal, `{param}`) 竞争对：

| method | 占位模板 | 真实模板 | 竞争的具体路径 | 实测应答 |
| --- | --- | --- | --- | --- |
| GET | `/v1/nexus/jobs/admission-readiness` | `/v1/nexus/jobs/{job_id}` | `/v1/nexus/jobs/admission-readiness` | `503 operation_not_implemented`（占位） |

真实↔真实之间没有任何「字面段恰好等于某个 `{param}` 值」的同深度对，占位↔真实之间
除上表外也没有。也就是说：**除本 PR 意图内的这一对，没有任何已实现的
`method+template` 被占位接管。**

### 2.3 真实 Job 是否仍然返回真实 Job（实测，含改前对照）

同一次组装下逐条发请求（Job 服务注入，拿到一个真实 job 对象）：

| 请求 | HEAD `bd9d7d0e` | `origin/main` 同口径 |
| --- | --- | --- |
| `GET /v1/nexus/jobs/<真实 job_id>` | `200` 真实 Job | `200` 真实 Job |
| `GET /v1/nexus/jobs/admission-readiness` | `503 operation_not_implemented` | `404 job_not_found` |
| `POST /v1/nexus/jobs/admission-readiness` | `405 job_method_not_allowed`，`Allow: GET` | `405 job_method_not_allowed`，`Allow: GET` |
| `DELETE /v1/nexus/jobs/<真实 job_id>` | `405 job_method_not_allowed`，`Allow: GET` | 同 |

作者自述「改前该路径返回 `503 job_unavailable`」只在 **Job 面未装配**（`jobs=None`）时成立；
实测 `create_app(settings=...)`、`jobs=None` 时 HEAD 与 `origin/main` 分别为
`503 operation_not_implemented` / `503 job_unavailable`。装配后的真实语义是
`404 job_not_found`（`admission-readiness` 是一个合法的 `Identifier`，查不到该 Job）。
两种说法都比改前更准确，方向没问题，但报告里的改前事实要按装配状态分开说。

改动的运行时约束也被测试钉住了：把占位路由挪回最后一步，`tests/contract/` 里恰好 1 条变红
（`test_unimplemented_operation_answers_the_stable_error[getJobAdmissionReadiness]`，
实测差异 `- operation_not_implemented / + job_unavailable`）。所以这不是一条只靠注释维持的顺序。

### 2.4 「更长/更短」不竞争，「同深度」才竞争（实测）

| 请求 | 实测 | 说明 |
| --- | --- | --- |
| `GET /v1/nexus/jobs/admission-readiness/events` | `404 job_not_found` | 5 段，命中真实的 `{job_id}/events`，占位完全不参与 |
| `GET /v1/nexus/deletions/admission-readiness` | `503 operation_not_implemented` | 4 段，命中占位 `/v1/nexus/deletions/{deletion_id}` |
| `GET /v1/nexus/operations/admission-readiness` | `503 operation_not_implemented` | 4 段，命中占位 `/v1/nexus/operations/{operation_id}` |

Starlette 不做前缀匹配，段数不同就互不竞争；同深度内先注册者胜，没有「literal 优先」。

### 2.5 `assert_placeholder_ownership` 守住了什么，守不住什么

守卫（`unimplemented.py:165`）在装配完成后按 `(method, 归一化 template)` 统计 owner，
要求占位表里每一对的 owner 恰好只有占位模块自己，因此：

* 同一个 `method+template` 被真实路由重复注册 → 无论先后都 `RuntimeError`（实测两种顺序都抛）；
* 跨 template 的具体路径重叠 → **不检查**。

可复现反例（三个内存 app，不动仓库文件）：

| 场景 | 占位 first/real first | 守卫 | `GET /v1/nexus/deletions/plan-literal` 的应答 |
| --- | --- | --- | --- |
| A：占位 `/v1/nexus/deletions/{deletion_id}` + 真实字面路由 `/v1/nexus/deletions/plan-literal` | 占位在前 | **沉默** | `503 operation_not_implemented`（真实路由被吞） |
| B：同上 | 真实在前 | **沉默** | `200`（真实路由应答） |
| C：真实路由注册在占位**同一个** template 上 | 任一顺序 | `RuntimeError` | — |

结论：守卫对它声明的那一类（同 template）精确、顺序无关；对「顺序会不会让占位吞并
已交付操作」这一更广的问题它不设防，只有登记顺序与人工核对在兜底。本 PR 之后仓库里
该类重叠仍然只有第 2.2 节那一个意图内路径，**实测无副作用**。

### 2.6 面向 #863 的真实约束（P1 的依据）

占位移除后，字面路由能否应答只取决于注册顺序。用同一框架实测：

```
literal registered first          -> 200 {'handler': 'admission-readiness'}
dynamic {job_id} registered first -> 200 {'handler': 'getJob', 'job_id': 'admission-readiness'}
```

所以 #863 必须把 `GET /v1/nexus/jobs/admission-readiness` 注册在
`GET /v1/nexus/jobs/{job_id}` 之前，而当时代替现在这条保证（占位先挂载）的东西就没了。
`tests/contract/test_job_admission_readiness.py:337-345` 用一个错误前提（`Identifier` 排除
`-`）把这件事说成「不会混淆」，且它只断言契约文本与「路径里有 `-`」，**在 #863 落地时
无法发现字面路由被吞**。这是本报告唯一一条与未来正确性直接相关的必修项。

## 3. 「先声明未实现路由」是不是坏先例

**不是新先例，是既有机制的第 4 次使用。** 事实（契约首次声明 commit 由
`git log -S "operationId: <op>" --reverse -- contracts/speech-openapi.yaml` 取）：

| 操作 | 契约首次声明 | 后端首次出现 |
| --- | --- | --- |
| `getOperation`/`cancelOperation`/`createRealtimeSession`/`getRealtimeSession`/`cancelRealtimeSession`/`connectRealtime` | `3adfa48b`（contract freeze） | `7e539980`（#473/#474 占位路由） |
| `speechCreateDeletionPlan`/`speechCreateDeletion`/`speechGetDeletion` | `bce10a64`（契约完整性收口） | `7e539980`（同上） |
| `getJobAdmissionReadiness` | `bd9d7d0e`（本 PR） | `bd9d7d0e`（本 PR） |

对全部 9 条占位路径逐个 `git log -S '"<path>"' -- backend/src`，命中都只有 `7e539980`
一处（本 PR 的新路径只命中 `bd9d7d0e`），说明既有 9 条**从未有过真实实现再被撤下**：
契约先冻结整套公开面，实现在 #473 之前一直没有，由中央占位路由统一回答 503；此后
被实现的操作（`createVoiceDesign` 等）是从占位表**移出**，而不是反向撤实现。
因此本 PR 的「声明 + 占位、实现随 #863」与 #473 以来的做法同构，不引入新先例。

差异在于体量：这次冻结的不只是一条路径，而是 8 值 `cause` 枚举、`status` 的 iff、
`instances`/`capacity` 两个结构（`contracts/speech-openapi.yaml:6634-6719`）。
这不是缺点——`cause` 枚举被双向钉在 `createJob` 的声明上（第 4 节），越早冻结越难漂移；
但 #863 实现时若发现枚举需要增删，契约与生成物要一起动，属于可接受的前置成本。

**文档把「声明 + 占位」的关系说清了吗？** 说清了一半，但引入了新的自相矛盾：

* `:381-388` 确实写了「该路由冻结的 503 是 Job 面未装配时的 `job_unavailable`，占位错误
  只是它尚未装配时的回答」「报告本身未实现…因此占位路由先于 Job 路由挂载」——关系是讲了的；
* 但紧邻的 `:383-384` 仍是「6 条指向 `OperationNotImplemented`，三条沿用 `ServiceUnavailable`」，
  与新段落的「第 10 条也沿用」直接矛盾（实测 6 + 4 = 10）；
* `:424`「当前 9 条占位中 6 条…3 条沿用」同样过期；
* §6.1（`:356`）自己规定「条数以 `KNOWN_UNIMPLEMENTED_OPERATIONS` 为准，本文不复制」，
  而本 PR 在 §6.2 正文里写死了「10 条」；
* `docs/plans/delivery-tracking.md:36` 与 `docs/plans/delivery-issues.json:226` 仍写
  「#473…为 9 条未实现操作建立中央占位路由」——作为 #473 的时点陈述可以保留，但读起来像当前口径。

**判断：做法可接受，文档要修。** 若不接受这条路（等 #863 实现一起进契约），代价是
#863 的切片必须同时改契约、生成物、装配与测试，且 `cause` 枚举失去「先冻结、由测试钉住」
的窗口；本 PR 的选择在 #473 已确立的机制内更小风险。因此不建议要求「等实现一起进契约」，
但要求把 §6.2/§424 的计数改对，或者按 §6.1 的自我约定删掉具体条数。

## 4. 闭包是不是真的闭

### 4.1 手写清单的性质

`status`/`cause`/`detail` 的 iff 由 `allOf/if-then` 机器化（`speech-openapi.yaml:6696`），
而码表有 **4 处手写声明 + 1 处运行时表**：

| 位置 | 形态 | 是否派生 |
| --- | --- | --- |
| 契约 `cause` 枚举 | YAML | 权威声明 |
| `speech/domain/job_admission_readiness.py` `AdmissionReadinessCause` + `CAUSE_CODES` | 手写 | 否 |
| `interfaces/http/job_errors.py:206` `DEPLOYMENT_STATE_CODES` | 手写 | 否 |
| 测试内 `NON_DEPLOYMENT_STATE_CODES` | 手写 | 否 |
| `job_errors._ERRORS`（code→status） | 手写 | 否 |

所以作者第一轮承认的「`DEPLOYMENT_STATE_CODES` 是手工第二清单」**没有被根治**，
新增一个部署态码要同步 5 处（契约枚举、`AdmissionReadinessCause`、`CAUSE_CODES`、
`DEPLOYMENT_STATE_CODES`、`_ERRORS`）。根治的办法只有「从契约派生」，但那会让
「两个独立声明互相校验」退化成「自己校验自己」，得不偿失。

真正的问题是**单边编辑会不会静默**。逐项突变实测（每项跑完立即按字节复原，
`git status --porcelain` 收尾为空，`__pycache__` 每项清理）：

| 突变 | 红 | 变红的测试 |
| --- | --- | --- |
| M1 契约 `cause` 枚举删 `storage_unavailable` | **2** | `test_the_contract_cause_enum_is_the_frozen_cause_vocabulary`、`test_the_declared_partition_covers_every_create_code` |
| M1b 同上但**先重生成**契约产物 | **2** | 同上（证明红不是生成漂移带来的） |
| M2 `createJob` 的 503 枚举加一个没有 cause 的码 `brand_new_deployment_code` | **1** | `test_the_declared_partition_covers_every_create_code` |
| M3 429 改回共享 `TooManyRequests` | **2** | `test_the_declared_partition_covers_every_create_code`、`test_the_capacity_answer_enumerates_its_code` |
| M4 `DEPLOYMENT_STATE_CODES` 删 `job_capacity_exhausted` | **2** | `test_the_causes_are_exactly_the_declared_deployment_state_answers`、partition |
| M5 `DEPLOYMENT_STATE_CODES` 加一个请求型码 `recipe_not_admitted` | **2** | 同上 |
| M6 测试里的 `NON_DEPLOYMENT_STATE_CODES` 删 `job_request_expired` | **1** | partition |
| M7 契约里 `createJob` 的 503 删 `engine_not_ready`（cause 仍指向它） | **2** | `test_every_cause_names_a_create_code_the_create_operation_declares[engine_receipt_expired]`、partition |

结论：作者说「删一个 cause 枚举码红 2 条」属实。**闭包真的闭**：往契约加一个新码会红
（M2），删部署态清单的一个码会红（M4），往部署态清单加一个不该在里面的码也红（M5），
把 429 退回共享引用让「声明了容量码」变成恒真同样红（M3）。作者自述的「全部 28 码 = 8 部署态
+ 20 非部署态、两边不相交」我也独立复算过：`createJob` 在 422/429/500/503 上枚举 19+1+1+7 = 28，
与两个手写集合的并集逐码相等。

残留（不阻断，记录在此）：

* 划分只覆盖 `createJob` **枚举过**的状态。`createJob` 另有 400/401/403/404/405/409/413 七种
  `$ref` 共享响应，码不可枚举；若将来把一个部署态码挂在这几个状态上，闭包看不见。
* `domain/job_admission_readiness.py` 里的 `CAPACITY_EXHAUSTED_CODE` 等是把 HTTP 码抄成
  字面量（domain 不能反向依赖 interfaces，这是刻意为之）。它们同样没有派生，只是被
  `_status_of`/`CAUSE_CODES` 的比较钉住；改名只改一处会红（M4/M7 已覆盖同类）。

## 5. `allOf/if-then` 四组实测（跑校验器，不只看文本）

三层独立校验：生成产物 `packages/contracts/src/speechnexus_contracts/file.schema.json`
的 `AdmissionReadiness`（`Draft202012Validator` + `FormatChecker`）、生成消费者入口
`speechnexus_contracts.parse`（其 `validate` 就是对该 bundle 的 jsonschema 校验）、
以及源契约 `contracts/speech-openapi.yaml`（带外部 `$ref` 的 registry）。

| 组合 | 期望 | 生成 bundle | `parse()` | 源契约 |
| --- | --- | --- | --- | --- |
| `admitting` + `cause:null` + `detail:null` | 接受 | 接受 | 接受 | 接受 |
| `degraded` + `cause` + `detail` | 接受 | 接受 | 接受 | 接受 |
| `admitting` + `cause` | **拒绝** | 拒绝 | 拒绝 | 拒绝 |
| `degraded` + `cause:null` | **拒绝** | 拒绝 | 拒绝 | 拒绝 |
| `admitting` + `detail` | **拒绝** | 拒绝 | 拒绝 | 拒绝 |
| `degraded` + `detail:null` | **拒绝** | 拒绝 | 拒绝 | 拒绝 |
| `admitting` + `cause` + `detail` | **拒绝** | 拒绝 | 拒绝 | 拒绝 |
| `status:"DEGRADED"`（枚举外） | **拒绝** | 拒绝 | 拒绝 | 拒绝 |

`file.schema.json` 里 `allOf` 两条 if-then 逐字保留（`status` 的 `const` + `required`、
`then.properties.cause/detail` 的 `type`），生成器没有把它折叠掉；`cause` 的可空分支是
`oneOf: [{type: null}, {type: string, enum: [...]}]`。四组非法组合在生成校验器上全部被拒，
**约束是真的生效**，不是只有 schema 文本。

## 6. 生成物零手改

```bash
UV_NO_CONFIG=1 make contracts-generate   # → Contract bindings: generated
UV_NO_CONFIG=1 make contracts-drift      # → Contract bindings: up to date
git status --porcelain                   # → 空
```

重生成前后对 8 个文件逐个 `sha256sum` 对比，**全部逐字节相同**（含
`contracts/bindings.json`、`packages/contracts/source-inventory.json`、
`speech.generated.ts`、`file.generated.ts`、`speechnexus_contracts/{file.schema.json,models.py,parsers.py}`、
`file.validators.generated.mjs`）。`tests/fixtures/contracts/file-workflow.json` 的 +162 行
同样由该生成器产出（生成器会写这个 fixture），`contracts-generate` 后无差异。

结论：`packages/contracts/**` 的 7 处变更全部是生成器输出，没有手改痕迹，且可重生成。

## 7. 实跑与基线

本机没有隔离 PostgreSQL / 对象存储 / 真实音频设备，所以**作者自称的 11585 passed 无法在本机复现**；
全量在本机是 `11642 passed, 58 failed, 294 skipped, 2382 errors`，抽样确认失败是环境前置
（`pytest.fail("Configure an isolated PostgreSQL DSN; skip is not migration evidence")`、
前端探针的 node 模块缺失），不是本 PR 引起。必需范围（`tests/contract` + `tests/ci` +
`tests/unit/backend`）在本机是完整可跑的，前后值自己取：

| 范围 | `origin/main` `c51c5443` | HEAD `bd9d7d0e` | 差 |
| --- | --- | --- | --- |
| `tests/contract`+`tests/ci`+`tests/unit/backend` | **7689 passed**，1648 subtests | **7715 passed**，1648 subtests | +26 |
| `tests/contract/test_job_admission_readiness.py` | 不存在 | 19 passed | +19 |
| `tests/contract/test_generated_bindings.py` | — | — | +6（`bindings.json` 多一个 `AdmissionReadiness` 绑定） |
| `tests/contract/test_unimplemented_operations.py` | — | — | +1（`CASES` 9→10） |

两次全绿、0 failed；用 `--collect-only` 的 node id 集合做差，+26 恰好由上面三项组成、无删除。
`scripts/dev-env/check-route-conformance.py` 退出码 0（`known_unimplemented` 10 条，无新偏差）；
`make contracts-http-changes` / `make contracts-speech-changes`（`CONTRACT_BASE_REF=origin/main`）
均 `Known HTTP structural checks: passed`——新增路径与 429 枚举收窄都没被结构门禁判为破坏。

## 8. 其他观察（不阻断）

* **新 domain 模块暂时没有生产消费者**：`speech/domain/job_admission_readiness.py` 只被测试
  导入（`grep -rn job_admission_readiness` 除自身以外命中仅 `job_errors.py` 的注释）。
  这与「词表先冻结、投影随 #863」的声明一致，不是死代码问题，但 #863 必须复用它而不是
  重写一份比较逻辑——模块 docstring 已写明这一点。
* **契约版本未 bump**（`0.1.0.dev18` 未变）。查历史：`27c945d0` 之后有 6 个 PR 改过
  `speech-openapi.yaml` 都没 bump 版本，属于既有惯例，不作为问题。
* **占位路由不出现在 serve 的 OpenAPI 里**（`include_in_schema=False`）：实测
  `app.openapi()["paths"]` 无 `/v1/nexus/jobs/admission-readiness`，58 个 operation 里也没有
  `getJobAdmissionReadiness`；与「部署不得宣告自己做不到的操作」一致。
* `OperationNotImplemented` 组件描述里的「five operations」在 `origin/main` 就已与 3 条
  占位（现为 4 条）不符，属既有文本，本 PR 未触碰，单独记录。

## 9. 必须修清单

1. `tests/contract/test_job_admission_readiness.py:341-345`：删掉「`Identifier` 排除 `-`」的
   错误论证（`contracts/shared/identifiers.schema.json:9` 的 pattern 明确允许 `-`，真实 job id
   形如 `job-<hex>`），把 `<` 换成真实约束——#863 必须把字面路由注册在 `{job_id}` 之前，
   否则被 `getJob` 吞掉；`assert "-" in PATH.rsplit("/", 1)[1]` 证明不了任何事，应替换为
   能表达该约束的断言（例如与 `jobs.py` 的注册顺序/占位表移除后的路由断言绑定）。
2. `docs/operations/contract-conformance-audit.md:381-384`：把「6 条 + 三条」改成实测的
   6 + 4，或按 §6.1 的自我约定不再写具体条数；`:424` 的「当前 9 条…6 条…3 条」同步。
3. （建议，同轮可做）`backend/src/speechnexus_backend/app.py:727-734`：把注释限定为守卫真正
   覆盖的范围（同一 `method+template`、顺序无关），不要让人以为「顺序吞并」已被守住；
   若要真守住，加一条「真实路由的任一具体路径不得被 `is_unimplemented_path` 命中」的断言。

## 第二轮：返修核实（`f73be9e8`）

返修 head `f73be9e8`（父 `bd9d7d0e`，单 commit `fix: 让「字面路由先于模板」成为装配期断言并去掉第二份手写数字`），
以 squash 合入 `eb894b7b`（基 `25c96360`）。核对 `git diff f73be9e8 eb894b7b -- backend tests/contract
tests/ci/test_doc_statements.py docs/operations/contract-conformance-audit.md docs/prd.md contracts packages
tests/unit scripts` 为**空**：返修内容即合入内容；合入树与 `f73be9e8` 的其余差异只来自主干 #871 的三个文件
（compose 文档与用例）。本节是增量记录，不修改第 1 节 `bd9d7d0e` 时点的结论。

### 逐条核对（我自己验的，不引用作者与主审的转述）

| # | 我上一轮的发现 | 返修做了什么 | 我的核实 |
| --- | --- | --- | --- |
| 1 | P1：`test_the_report_path_is_declared_and_not_shadowed_by_the_identifier_route` 用「`Identifier` 排除 `-`」论证两条路由不会混淆，既错又掩盖了注册顺序 | 删该测试，换成三条：契约 + 占位表断言；运行时两码对照；`{job_id}` 先注册的控制组 | `test_job_admission_readiness.py` 19→21 passed，三条全绿；注释已写明 `Identifier` 允许 `-`、真实 job id 是 `job-<hex>`、「只能由 Starlette 先到达哪条路由」决定；控制组断言 `assert_placeholder_ownership` 抛出含 `answers /v1/nexus/jobs/admission-readiness` 的消息，实测成立 |
| 2 | P2：`assert_placeholder_ownership` 对跨 template 的路径重叠沉默，装配注释把能力说大了 | 新增 `sample_path`/`declared_methods` 与 `shadowed` 推导：按注册顺序遍历**全部**路由，用每条路由自己的模板填出具体路径，方法相交且被更早注册的路由命中就失败；注释同步收窄 | **是通用的**：`unimplemented.py` 里 `admission` 只出现在占位表项（`:89`）与 docstring 示例（`:222`），没有 operationId/路径分支。我另造一对与 #845 无关的真实路由——`/v1/nexus/widgets/{widget_id}` 先注册 + `/v1/nexus/widgets/plan-literal` 后注册 → 抛「an earlier route must not answer the requests a later route serves」；反序 → 沉默。检查对「真实↔真实」同样生效，不是给这一条打的特例 |
| 3 | （同上，红侧）顺序被测试钉住，但原守卫只覆盖同 template | 移除占位条目时装配期就要失败 | 我做了两个方向的变异（临时改 `app.py`，按字节复原，`git status` 收尾为空，`__pycache__` 清理）：**B1** 把 `include_router(create_unimplemented_router())` 挪到 `return app` 之前（即守卫生效之后）→ `create_app` 直接 `RuntimeError: real routes must not share a method+template with a placeholder; placeholders must be the only owner: GET /v1/nexus/deletions/{deletion_id}, GET /v1/nexus/jobs/admission-readiness, …`（缺失的占位全部列出，共 10 条），进程退出码 1；**B2** 挪到全部 feature router 之后、守卫之前 → `RuntimeError: an earlier route must not answer the requests a later route serves; register the more specific template first and remove a placeholder once its operation ships: /v1/nexus/jobs/{job_id} answers /v1/nexus/jobs/admission-readiness`，退出码 1。两种挪法都在**装配期**炸（`create_app`），不靠测试兜；B1 命中的是「占位未进表」分支，B2 才是新的有序重叠分支 |
| 4 | P1：审计文档 `:381` 说 10、`:383-384` 与 `:424` 说 9，实测 6+4 | 两处数字全删，改为指向 `KNOWN_UNIMPLEMENTED_OPERATIONS` 与契约 | `grep -nE "(9|10) 条"` 只剩 `:346`（`createVoiceDesign`…`publishVoiceDraftRevision` 这 9 条 **#473 批次名单**）与 `:39/:41` 的审计基线路径/操作计数；当前占位条数一处不剩。`grep "当前[^。]*条"` 只剩 `:381`「§6.1 常量里的每一条」与 `:425` 带【计数为 #473 交付时口径】前缀那句——两处都是指向常量的表述。`:358`（§6.1）原本的「成员、条数与原因以该常量为准，本文不复制」现与 `:383`、`:425` 口径一致 |
| 5 | P3：占位期 `job_unavailable` 分支不可观测 | 写进审计文档 §6.2 | 原文现为「占位期两种装配态实测都答占位错误，所以 `job_unavailable` 这一分支要等实现装配后才可观测」；与本轮复测一致：Job 面装配时字面路径 `503 operation_not_implemented`、真实 job id `404 job_not_found`；`jobs=None` 时字面路径仍 `503 operation_not_implemented`、job id 形路径 `503 job_unavailable` |

### 我上一轮四条「必须修」的状态

| 上一轮条目 | 状态 |
| --- | --- |
| P1 测试：错误的 `Identifier` 论证 + 未表达的真实约束 | **已修**。测试被替换为契约断言 + 运行时两码对照 + 控制组，且约束写进 `app.py:731-737` 注释与 `assert_placeholder_ownership` docstring |
| P1 文档：§6.2 与 `:424` 的计数自相矛盾 | **已修**。当前占位条数不再手写，三处统一指向门禁常量与契约 |
| P2 注释：守卫能力被说大 | **已修且加强**。不是只改注释：守卫升级为通用有序重叠检测，两个方向各有一条测试；注释收窄到真正覆盖的宽度 |
| P3 可选：占位期 503 语义 | **已修**（文档写明，见上表第 5 行） |

### 仍未解决的（`#863` 的输入，写清交给谁）

1. **报告实现未装配**：`GET /v1/nexus/jobs/admission-readiness` 在两种装配态都返回 `503 operation_not_implemented`；树内没有生产者——`job_admission_readiness` 的引用只有模块自身 docstring、`job_errors.py` 的注释与测试。
2. **轮询者随 `#863` 落地**：`eb894b7b` 树内没有 `admission_watch.py`，`deploy/compose.yaml` 也没有 admission-watch 服务（#857 未合入）；全树提到该路由的 12 个文件里没有消费者。因此占位期没有任何客户端会因 503 而坏，报告与轮询者要一起交付。
3. **占位期没有 `200` 生产响应**：契约声明 `200` + `AdmissionReadiness`，运行时尚无任何路径返回 200（本轮在装配/未装配两种状态下复测）。

三条都交给 **#863**（OPEN：「准入可观察面 B：选择层与准入层收敛成一个函数，报告走同一路径（拆自 #845）」）。
另记一条簿记：拆出的 A 片 **#862** 在 PR 合入后仍是 OPEN，不影响本 PR 的代码结论，但应在 `#863` 收口时一并核对。

### 残留问题的变化

* **「闭包只覆盖 `createJob` 枚举过的状态」——没有变化。** `DECLARED_CREATE_STATUSES = (422, 429, 500, 503)` 在返修中未改；`createJob` 的 400/401/403/404/405/409/413 仍是共享响应、码不可枚举，挂在这些状态上的部署态码依旧不可见。
* 返修引入了两个新的装配期性质（记录、不阻断）：有序重叠检查对**整张表**生效，装配期是 O(n²) 的有序配对（初版实测 72 条路由、返修未增删路由，即约 2.5k 次模板匹配，实测无感）；它同时把「真实路由 `method+template` 重复」也变成装配失败（旧实现只查占位对），属收紧——若将来需要一条刻意不可达的别名路由，会被这条挡住，需显式设计。

### 实跑（前后值我自己取）

| 提交 | 范围 | 结果 |
| --- | --- | --- |
| `25c96360`（合并前 main） | `tests/contract`+`tests/ci`+`tests/unit/backend` | **7691 passed**，1648 subtests |
| `eb894b7b`（合并后 main） | 同上 | **7721 passed**，1648 subtests |
| `f73be9e8` / `eb894b7b` | `test_job_admission_readiness.py`+`tests/ci` | **478 / 480 passed**，1642 subtests |

* `+30` = 初版 `+26`（19 条新用例 + 6 条生成绑定 + 1 条占位用例）+ 返修 `+4`（readiness 文件 −1+3、`test_unimplemented_operations.py` +2）；两次全绿、0 failed。
* 主审的 478 与我在合并后测得的 480 差 2，来自主干 #871 在 `tests/ci/test_compose_minimal_role_provisioning.py` 新增的 2 条用例（`git diff f73be9e8 eb894b7b` 该文件 `+2` 个 `def test_`），不是本 PR 成分。
* 文件级：`test_job_admission_readiness.py` 21 passed（19→21）、`test_unimplemented_operations.py` 39 passed（37→39），两文件合计 60 passed。
* 本轮变异只在 `/private/tmp/sn872fix` 的临时检出内进行，逐项按字节复原并清理 `__pycache__`；本分支相对 `origin/main` 仍只有这一份报告文件。
