---
title: "PR #839 返修非作者审查：readiness 错误码整类返修（#828）"
status: active
version: "0.1.0"
date: 2026-11-15
---

# PR #839 返修非作者审查：readiness 错误码整类返修（#828）

审查对象：PR [#839](https://github.com/hrygo/SpeechNexus/pull/839)
`fix/828-readiness-expiry`，head `e5ef03f68655f74ad9175d4dcceb9132d469000e`（#828 的返修）。
本卡是第一轮报告 [`2026-11-15-readiness-error-code.md`](2026-11-15-readiness-error-code.md)
（已在 main）中 **B1–B4** 四条必须修的返修审查。

被审内容 70 文件、+3185/−138；最后一笔 `e5ef03f6` 只重排了收窄响应的形状（+288/−37）。
审查分支 `review/849-readiness-rework` 基于 `origin/main`，本报告是它与 `origin/main`
的唯一差异。

## 结论

**B1、B2、B4 返修成立；B3 的主干（折叠表）成立，但「兜底是否真的没了」的答案是「没完全没」——
同一响应仍有三条通道能把未登记码还原成 #828 那段回答，而且新增的两条结构性守护都可以被绕过，
其中一条我按本卡要求构造后实测为「假绿」。另有两处新增行为需要改：voice-draft 提交面把引擎侧
原因改成 422 用户输入错误，以及 workbench 的 `unreplayable` 分支给 `job_request_expired`
复用了 413 的「请求体超限」文案。**

逐条要点：

1. **`rejection_code()` 本身干净**：只有 `if code in CAPACITY_REJECTION_CODES` + `return
   _PUBLIC_CODES[code]`，无默认值、无 `.get(…, default)`、函数体内无 `try`（§A1）。
2. **但三处仍能复现原缺陷形态**（全部实测）：
   `job_error()` 的 `code = "job_unavailable"` 默认（`job_errors.py:195`）；
   `_raise_admission_rejection()` 对未登记码静默放行后落 `JobRejectedError("contract")`
   → `job_error("job_unavailable")`（`public_jobs.py:1504-1509`）；
   voice-draft 面 `except (…, KeyError, …)` 把 `rejection_code` 的 `KeyError` 吞成
   503「Voice draft storage is unavailable.」+ **`retryable: true`**（`voice_drafts.py:427-436`）。
   第三条比原缺陷更危险：原缺陷至少是 `retryable: false`（§A2）。
3. **AST 守护可绕过（两条构造都实测 26 passed 假绿）**：变量传码；未扫描模块里的新 raise 点。
   而 `ADMISSION_MODULES` 只覆盖 13 个 raise 该三类错误的模块中的 6 个（§A4）。
4. **守护的强度分层**：「删一行 → 红」与「新增词汇表成员 → 红」都成立（§A3）；
   但「新增公开码只登记一半 → 绿」不成立（§A2 实测）。
5. **契约枚举覆盖正确、无假承诺**：9 个公开码 8 个落在收窄状态且全在 enum（第 9 个是 409，
   未收窄，作者已声明）；反方向 19/1/7 项全部状态一致；`allOf` 形状与原 `$ref` 语义等价且更严；
   删枚举项 → 3 条测试真红（§B5–B8）。**但反方向守护只校验 `_ERRORS` 的状态，不校验
   `createJob` 是否真会发出**，强度弱于自报（§B5）。
6. **`retryable` 通用规则已写入共享 schema，够用**；但同一字段仍并存一句旧定义（speech Job
   按「重放未知结果」计算），且有两个与通用规则相反的取值（§C9）。
7. **voice-draft 422 判断：是把一种错归因换成另一种错**（§D10，实测五码全落
   `422 voice_draft_input_unavailable` + "Published model or ready input required."）。
8. 例数与 CI 与自报一致（唯一偏差是 `tests/unit` 自报 7164 是 passed 口径，collected 为 7167；
   两种口径的差值都是 +21），`gh pr checks 839` 的 run head sha 确为 `e5ef03f6`，
   27 pass / 1 skipping / 0 fail（§E12、§E13）。

## 环境

| 项 | 值 |
| --- | --- |
| 主机 / 时间 | macOS arm64，2026-11-15（Asia/Shanghai），18:11 起 |
| 审查 worktree | `/private/tmp/sn849`，分支 `review/849-readiness-rework`（基于 `origin/main`） |
| 被审代码 worktree | `/private/tmp/sn849-run`，detached 于 `e5ef03f6`（只读上下文；变异只在此副本内，逐次 `git checkout --` 复原，收尾 `git status` 为空） |
| Python / pytest / pnpm | 3.14.9 / 9.1.1 / 12.14.0 |
| PostgreSQL | 一次性容器 `sn849-pg`（`postgres:16.15-alpine`，`127.0.0.1:55449`） |
| MySQL | 一次性容器 `sn849-mysql`（`mysql:8.4.8`，`127.0.0.1:55450`） |
| DSN | `SPEECHNEXUS_TEST_DATABASE_URL`、`SPEECHNEXUS_TEST_MYSQL_URL` |

两个容器在审查结束时 `docker rm -f`；未触碰 `sn838-pg`、`sn824-pg`、`sn824-minio`、
`speechnexus-coordination-mysql`、`sn832-workbench-api-1` 等既有实例，未停止或修改任何共享
GPU 上的引擎，未 push main，未合并。

> 审查期间 `origin/main` 从 `53469b24` 前进到 `c9a48963`（#842）。我把报告分支 rebase 到最新
> `origin/main`；§E12 的基线在 merge-base `6a4d7184` 与最新 main 上实测相同
> （404 / 3815 / 7146 / 3273），不影响任何判定。

## 时间线（过程记录）

| 时刻 | 动作 | 结果 |
| --- | --- | --- |
| 18:11 | 建 `sn849`（基于 `origin/main`）与 `sn849-run`（detached `e5ef03f6`）；确认 `docs/reviews/2026-11-15-readiness-error-code-rework.md` 不存在；读第一轮报告 | 文件不存在，可新建 |
| 18:12 | 读 `rejection_code` / `_PUBLIC_CODES` / `ADMISSION_REJECTION_CODES` / `_ADMISSION_REJECTION_CODES` / `job_error` / `_raise_admission_rejection` 全链 | 定位三处残余通道 |
| 18:13 | 读新增守护 `tests/contract/test_public_job_admission_error_vocabulary.py`；全仓 grep 三类拒绝错误 raise 点 | 发现模块清单只覆盖 6/13 |
| 18:14 | `uv sync --frozen --all-packages`；起 `sn849-pg`；基线跑 HTTP 集成（9 passed） | 环境就绪 |
| 18:15 | 采集 PR head 与 `origin/main` 的四套例数 | 414/3853/7167/3294 与 404/3815/7146/3273 |
| 18:16 | `make contracts-check` / `contracts-drift` / `contracts-speech-changes` / `contracts-route-conformance` / `contracts-http-changes` | 全绿，exit 0 |
| 18:19 | **A2b**：`workspace_not_configured` 改未登记公开码 → 真 PG + 生产装配 POST | `503 job_unavailable`「Job storage is unavailable.」（实测） |
| 18:20 | **A2c**：`expired_code` 改未登记词汇表码 → `KeyError` → `raise_app_exceptions=False` 探针 | `500 Internal Server Error`（无信封，实测） |
| 18:21 | **A3-1** 删 `_PUBLIC_CODES` 一行；**A3-2** 往 `ADMISSION_REJECTION_CODES` 加成员 | 各 1 failed（真红） |
| 18:22 | **A4-1** 未扫描模块 `runtime_intents.py` 换未登记码；**A4-2** 扫描模块内变量传码 | 两次均 26 passed（假绿） |
| 18:23 | **B8** 删 503 enum 的 `engine_not_ready` | 3 failed（含 HTTP 信封校验） |
| 18:24 | `tests/unit tests/contract tests/ci` 全量；`ruff check` / `mypy` / `lint-imports` | 11431 passed, 3 skipped, 1357 subtests；全绿 |
| 18:26 | workbench `vitest`、admin `vitest`；起 `sn849-mysql` | 1070 passed、151 passed |
| 18:27 | 集成三件套（HTTP admission + engine_registration + production_admission） | 86 passed |
| 18:28 | **D10 探针**：voice-draft 提交面注入引擎侧码与 `KeyError` | 五码全 422 `input_unavailable`；`KeyError` → 503 `unavailable` retryable true |
| 18:29 | **M1(a) 探针**：把 `engine_retired` 只加进 `_ADMISSION_REJECTION_CODES` | 守护 26 passed；HTTP 回答 `503 job_unavailable`（实测） |
| 18:30 | `gh pr checks 839` + `gh api .../actions/runs/43701451751` | 27 pass / 1 skipping；run head sha = `e5ef03f6` |
| 18:31 | 复原全部变异、删探针、`git status` 为空；`origin/main` 前进后 rebase 报告分支 | 无残留 |
| 18:32 | 写本报告 | — |

## A. 兜底是否真的没了（B3，核心）

### A1 `rejection_code()` 本体：干净

`backend/src/speechnexus_backend/speech/infrastructure/public_job_admission.py:1015-1026`：

```python
def rejection_code(code: str) -> str:
    if code in CAPACITY_REJECTION_CODES:
        return "job_capacity_exhausted"
    return _PUBLIC_CODES[code]
```

函数体内没有 `try`、没有 `.get(…, default)`、没有宽泛 `except`，docstring 也把「失败要响」写成了
约束而不是愿望。**这一条我确认成立。**

`_PUBLIC_CODES` 对 15 个非容量码逐条给出公开答案，`CAPACITY_REJECTION_CODES`（2 个）走 429；
17 个准入码 → 9 个公开码（`idempotency_conflict`、`admission_state_invalid`、
`job_request_expired`、`engine_state_mismatch`、`job_admission_contended`、
`engine_resource_disabled`、`engine_not_ready`、`recipe_not_admitted`、`job_capacity_exhausted`）。
容量判断在映射之前，`quota_exceeded`/`capacity_exhausted` 仍落 429，没有被新码吞并。

### A2 残余回落：三条通道都能还原 #828 的响应（全部实测）

作者的表述是「未知内部码仍回落 `job_unavailable`（只可能来自存储层）」。**这句话不成立：
准入侧自己就能把一段回答变成伪装的存储故障，有三条路径，我逐条实测了。**

**(a) 公开码只登记在 `_ADMISSION_REJECTION_CODES`、没进 `_ERRORS`。**
`job_error()`（`job_errors.py:194-196`）仍保留

```python
if code not in _ERRORS:
    code = "job_unavailable"
```

实测：把 `"engine_retired"` 只加进 `public_jobs.py:1468` 的 `_ADMISSION_REJECTION_CODES`，
其余不动 → 生产装配 HTTP POST `/v1/nexus/jobs`：

```text
[PROBE FALLBACK] status=503 body={"error":{"code":"job_unavailable","type":"service_unavailable_error",
  "message":"Job storage is unavailable.","param":null,
  "request_id":"0c1fcc11f80f4bff8c537f87d278495d","retryable":false}}
```

**这正是 #828 报的那一段**，且新增的结构性守护对此**全绿**（见 A2 末尾）。

**(b) 准入侧的公开码没进 `_ADMISSION_REJECTION_CODES`。**
`_raise_admission_rejection(code)`（`public_jobs.py:1504-1509`）只在 `code in
_ADMISSION_REJECTION_CODES` 时才抛 `JobAdmissionRejectedError`；未命中时函数**静默返回**，
调用点的下一句是 `raise JobRejectedError(_rejection_reason(code))`，而 `_rejection_reason`
对任何未知码返回 `"contract"`，`"contract"` 在 HTTP 层是
`except JobRejectedError, PublicJobUnavailableError: return job_error("job_unavailable", …)`。

实测：把 `public_job_admission.py:223` 的 `PublicJobAdmissionRejectedError("workspace_not_configured")`
改成 `"workspace_quota_missing"`（真 PG + 生产装配）：

```text
E  AssertionError: {"error":{"code":"job_unavailable","type":"service_unavailable_error",
   "message":"Job storage is unavailable.","param":null,
   "request_id":"d9df8dbdb8a04f12a2d7f2353ec530d5","retryable":false}}
E  assert 503 == 422
```

**(c) `rejection_code` 的 `KeyError` 在 voice-draft 面被宽泛 `except` 吞掉。**
`voice_drafts.py:427-436`：

```python
    except (
        PublicJobUnavailableError,
        ValueError,
        KeyError,
        TypeError,
        RecursionError,
    ):
        return _error("unavailable", request_id, operation)
```

`_error("unavailable")` 是 `(503, service_unavailable_error, "Voice draft storage is unavailable.",
"unavailable", True)`。本卡明确要求「确认没有任何……宽泛 except 把 `KeyError` 吞掉」——
**准入侧没有，但消费者侧有**。实测（`validateVoiceDraftRevision`，注入 `KeyError`）：

```text
[PROBE D10] KeyError 'readiness_lapsed' -> 503 {"error":{"code":"voice_draft_unavailable",
  "type":"service_unavailable_error","message":"Voice draft storage is unavailable.","param":null,
  "request_id":"db6515392b684bdbbd1ff5fdc68d33a6","retryable":true}}
```

**比原缺陷更危险**：第一轮问题是「健康存储被报成 `job_unavailable` 且 `retryable: false`」，
这里是「健康存储被报成语音草稿存储不可用，且 `retryable: true`」，一个按契约重试的客户端会
在收据被重签之前持续放大流量——正是第一轮 A4 担心的那个场景。

**「未映射即 KeyError」这个断言本身是真的，但它的唯一实际后果是 jobs 面一个裸 500：**

**A2c 探针**（`expired_code` 由 `"readiness_expired"` 改成 `"readiness_lapsed"`，即准入模块
raise 一个不在词汇表里的码；真 PG + 生产装配，`raise_app_exceptions=False` 取最终响应）：

```text
[PROBE A2 UNLISTED-VOCAB] status=500
[PROBE A2 UNLISTED-VOCAB] body=Internal Server Error
```

即：伪装没了，但回答是 `text/plain` 的裸 500，**不是**契约新声明的
`application/json` `{"error":{...,"code":"admission_state_invalid"}}`。这对内部缺陷可接受，
但它同时说明「契约新增的 500 信封」与「未登记码的真实输出」并不是同一个东西。
（同一变异下 `tests/contract/test_public_job_admission_error_vocabulary.py` 仍是
26 passed——见 A4。）

### A3 集合相等守护的强度

**够用，且两条构造都真红。**

**A3-1 删一行**（删 `_PUBLIC_CODES` 的 `"readiness_expired": "engine_not_ready"`）：

```text
E       KeyError: 'readiness_expired'
FAILED tests/contract/test_public_job_admission_error_vocabulary.py::test_the_public_mapping_is_total_over_the_declared_vocabulary
FAILED tests/contract/test_public_job_admission_error_vocabulary.py::test_each_admission_cause_names_its_own_recovery[readiness_expired-engine_not_ready]
2 failed, 24 passed in 0.16s
```

**A3-2 往 `ADMISSION_REJECTION_CODES` 加成员、不加映射**（本卡指定的构造）：

```text
E       AssertionError: assert {'idempotency..._active', ...} == frozenset({'b...equest', ...})
E         Extra items in the right set:
E         'brand_new_admission_code'
FAILED tests/contract/test_public_job_admission_error_vocabulary.py::test_the_public_mapping_is_total_over_the_declared_vocabulary
1 failed, 25 passed in 0.15s
```

`assert set(_PUBLIC_CODES) == ADMISSION_REJECTION_CODES - CAPACITY_REJECTION_CODES` 这一句同时
覆盖「加词汇表成员」和「加多余映射」两个方向，比自报描述的更强一点。**这条守护本身合格。**

但它只覆盖「准入码 ↔ 公开码」这一跳；**「公开码 ↔ `_ERRORS`/契约 enum」这一跳没有守护**，
这正是 A2(a) 能过检的原因（A2(a) 的变异下 26 条全绿）。

### A4 AST 守护能否被绕过：能，两条都实测假绿，且清单不完整

`ADMISSION_MODULES`（`tests/contract/test_public_job_admission_error_vocabulary.py:47-54`）只有
6 个模块：`domain/admission.py`、`domain/owners.py`、`domain/runtime_observations.py`、
`infrastructure/admission_sql.py`、`admission_recovery_sql.py`、`admission_postgres.py`。

**清单不完整。** 我全仓 grep `AdmissionRejectedError(` / `OwnerRejectedError(` /
`RuntimeRejectedError(` 的 raise 点，非测试代码里还有 7 个模块会产生同类异常：

| 模块 | 是否有不在词汇表的码 |
| --- | --- |
| `speech/domain/runtime_intents.py` | **有**：`unapproved_runtime_template`、`gateway_drain_incomplete` |
| `speech/infrastructure/runtime_sql.py` | **有**：`unapproved_runtime_template` |
| `speech/infrastructure/owners_sql.py` | 否 |
| `speech/infrastructure/runtime_observation_sql.py` | 否 |
| `speech/infrastructure/qualification_admission_postgres.py` | 否 |
| `speech/infrastructure/tts_settlement_sql.py` | 否 |
| `speech/infrastructure/builtin_voice_qualification.py` | 否 |

那两个不在词汇表的码今天不在 create 路径上，但 `admission_sql.py:93-94` 会
`except RuntimeRejectedError as error: raise AdmissionRejectedError(error.code)` **原样透传**；
一旦某个被透传的函数改成抛这两个码之一，线上就是 `rejection_code` 的 `KeyError`（jobs 面裸 500），
而**扫描器看不到**——它只看被扫描模块里 `AdmissionRejectedError(...)` 的字面量。

**构造 1（未扫描模块的新 raise 点）**：把 `runtime_intents.py` 的一个
`RuntimeRejectedError("invalid_runtime_request")` 改成 `RuntimeRejectedError("quadrant_violation")`：

```text
..........................                                               [100%]
26 passed in 0.14s
```

**构造 2（扫描模块内变量传码）**：在 `domain/admission.py` 加模块常量
`_RECIPE_REFUSAL = "recipe_mismatch"`，把一处 `raise AdmissionRejectedError("recipe_mismatch")`
改成 `raise AdmissionRejectedError(_RECIPE_REFUSAL)`：

```text
..........................                                               [100%]
26 passed in 0.14s
```

`_raised_codes()` 只收集 `ast.Call` 里 `args` 为 `ast.Constant(str)` 的实参，所以
`AdmissionRejectedError(error.code)`（仓库里已有 3 处这种透传：`admission.py:363`、
`admission.py:380`、`admission_sql.py:94`）与任何变量/常量实参都不可见。
`test_the_scan_reaches_the_raise_sites` 只断言「并集非空」，无法发现某模块整体改成变量传码。

**判定：这条守护是最容易假绿的一点，本卡预判成立。** 它的实际价值是「防止新增字面量码」，
不是「防止新增码」。

## B. 契约枚举（B2，核心）

### B5 两个方向的覆盖

**正方向：9 个公开码逐条核。**

| 公开码 | 状态 | 该状态 enum 内 |
| --- | --- | --- |
| `engine_not_ready` | 503 | ✓ |
| `engine_state_mismatch` | 503 | ✓ |
| `engine_resource_disabled` | 503 | ✓ |
| `recipe_not_admitted` | 503 | ✓ |
| `job_admission_contended` | 503 | ✓ |
| `job_request_expired` | 422 | ✓ |
| `admission_state_invalid` | 500 | ✓ |
| `idempotency_conflict` | 409 | 409 未收窄（共享 `Conflict` 组件，作者已声明） |
| `job_capacity_exhausted` | 429 | 429 未收窄（`$ref: TooManyRequests`） |

计数与自报一致：503 七码、422 十九码、500 一码。

**反方向：枚举里的码是否真会用该状态发出。**

- 19/1/7 个枚举项逐个核 `_ERRORS`，状态**全部一致**，没有一项对不上（我脚本枚举过，无差异行）。
- 但 `test_the_narrowed_statuses_do_not_advertise_codes_they_cannot_return` 的实际断言是
  `assert public in _ERRORS` + `assert str(_ERRORS[public][0]) == status`。`_ERRORS` 是**整个
  speech Job 面共用的表**（create/cancel/retry/get/list 共用），所以这个断言只证明
  「不同操作之间状态一致」，**不证明「`createJob` 会发出它」**。自报说「反方向也校验（枚举里
  的码必须是后端真会用该状态发出的）」——**事实成立，但守护比这句自报弱。**
- 我逐条把这些码追到 `jobs.py` 的 `create` 处理器：`job_kind_not_supported`(:483)、
  `workspace_not_found`(:485)、`workspace_disabled`(:487)、`model_not_published`(:489)、
  `ModelBindingError` 的 5 个码(:491)、`voice_not_published`、`profile_not_published`、
  `pronunciation_not_found`、`asset_not_ready`、`workspace_not_configured`、
  `job_binding_incomplete`、`job_text_asset_unsupported`、`voice_draft_not_candidate` 各有
  except 分支，`job_request_expired`/`admission_state_invalid` 来自 `JobAdmissionRejectedError`。
  **19 个 422 码都有 emit 点，没有假承诺。**
- 覆盖强度不均：`engine_not_ready`、`engine_state_mismatch`、`job_unavailable`、
  `workspace_not_configured` 有真 PG + 生产 HTTP 往返用例；
  `engine_resource_disabled`、`recipe_not_admitted`、`job_request_expired`、
  `admission_state_invalid` 只有映射级 + `_ERRORS` 级证据（见建议 S3）。

**判定：枚举无假承诺；反方向守护比自报的弱一档，属建议修。**

### B6 `allOf` 写法是否正确

三处收窄（422/500/503）的形状是：

```yaml
schema:
  type: object
  additionalProperties: false
  required: [error]
  properties:
    error:
      $ref: "./shared/errors.schema.json#/$defs/ErrorBody"
  allOf:
    - $ref: "#/components/schemas/ErrorResponse"
    - properties:
        error:
          properties:
            code:
              enum: [...]
```

- **不是「为了过检查器而放弃枚举」**：`allOf[1]` 的 `code.enum` 是真实约束。删掉
  `engine_not_ready` 后 `test_create_answers_the_declared_envelope_over_http` 用
  `assert_error_contract("createJob", response)` 对真实响应做 JSON Schema 校验并变红
  （见 B8），说明枚举在**解析后的 schema 上真的生效**，不是装饰。
- **形状与原 `$ref` 等价且更严**：`shared/errors.schema.json#/$defs/ErrorResponse` 的定义就是
  `{type: object, additionalProperties: false, required: [error], properties: {error: $ref
  ErrorBody}}`，与内联部分逐字段相同（我直接读了 `$defs` 比对）。`allOf[0]` 仍引用它，
  所以是「同一约束写两遍 + 追加 enum」，语义等值、约束更强。
- **保留的内联形状是为通过结构检查器**（`make contracts-speech-changes`）：该检查器按解析后
  形状比对，只留 `allOf: [$ref]` 会报 `type changed from ['object'] to []`。这是可核实的理由，
  而且我实跑得到 `Known HTTP structural checks: passed`。代价是**同一结构在 7 处内联复制**，
  将来 `ErrorResponse` 加字段时这些副本不会自动跟随（建议 S4）。
- 生成的 TS 侧确实是机器可读的联合类型（`speech.generated.ts` 里
  `code?: "unsupported_parameter" | … | "job_request_expired"`），消费者可以枚举，B2 的目的达到。

### B7 生成物与 drift

四项独立证据：

1. `packages/contracts/source-inventory.json` 记录 `speech-openapi.yaml` 的 sha256
   `4a2fcedb1bf4c46abd5252ec3d5ffb1bdcb4bafc28ed0bbdd00475f176be74dc`，与
   `shasum -a 256 contracts/speech-openapi.yaml` 实测**完全一致** → 清单是生成器重算的。
2. `make contracts-drift`：

   ```text
   Found 36 errors (36 fixed, 0 remaining).
   Contract bindings: up to date
   ```

3. `make contracts-check`：`contract validation: passed (37 files)`，exit 0。
4. `CONTRACT_BASE_REF=origin/main make contracts-speech-changes`：
   `Known HTTP structural checks: passed`，exit 0；`make contracts-route-conformance`：
   `OK: every registered speech route matches the contract`；
   `make contracts-http-changes` 同样 passed。

**没有被手改的迹象。** 唯一值得记的：结构检查器的自述未证范围里明确包含
`added statuses/media`，而本 PR 给 `createJob` 新增了 500 状态——这处新增状态不在该检查器的
证明范围内（建议 S5）。

### B8 反例：删枚举项 → 红

把 503 enum 的 `engine_not_ready` 删掉：

```text
E           On instance['error']['code']:
E               'engine_not_ready'
FAILED tests/contract/test_public_job_admission_error_vocabulary.py::test_every_mapped_public_code_is_declared_on_its_status
FAILED tests/contract/test_public_job_engine_readiness.py::test_the_operation_declares_the_readiness_refusal_with_its_recovery
FAILED tests/contract/test_public_job_engine_readiness.py::test_create_answers_the_declared_envelope_over_http
3 failed, 35 passed in 0.39s
```

其中第三条是真实响应体的 schema 校验失败——这是本项最强的证据。

## C. `retryable` 语义（B1）

### C9 共享 schema 的描述与全仓取值

**改动内容**（`contracts/shared/errors.schema.json` 的 `ErrorBody.retryable.description`
末尾新增一句）：

> It answers only 'can this same request succeed without an out-of-band change?': a recovery that
> needs an operator step -- provisioning the workspace quota, re-signing an engine readiness
> receipt, restoring a disabled instance, re-registering against an approved recipe -- is false
> even though the recovery is known and repeatable, and the recovery itself is named by the
> operation's published code and message rather than by this field.

**判断：这条规则够通用，后人能照着填值**——它给的是判据（有无带外改动）而不是本码的复述，
并顺手给了四个例子和「恢复写在 code/message 里」这条配套约定。B1 的目的达到。

**但同一字段现在并存一句与新规则冲突的旧定义。** 同一段文字里仍写着：

> the speech Job operations compute this field from the narrower question 'may an unknown
> execution outcome be replayed?'

返修后这句话**已经不成立**：`job_capacity_exhausted`（429，等租约释放）与
`job_admission_contended`（503，并发竞争）都是 `true`，与「重放未知结果」毫无关系。
`contracts/workbench-openapi.yaml:2278-2284` 也重复了同一句旧定义。**同一字段两张规则并存，
且其中一张对已改的那一面是错的。**

**取值冲突（逐个状态响应扫过）：**

| # | 位置 | 值 | 与新规则的关系 |
| --- | --- | --- | --- |
| 1 | `job_errors.py` `execution_cleanup_pending`（409） | `false` | **冲突**：等清理完成不需要带外动作，按新规则应为 `true`；旧句用「更窄的问题」解释它，而新规则没有例外条款。workbench 把同一情形（`render_submission_cleanup_pending`）标 `true` |
| 2 | `job_errors.py` `job_unavailable`(503 存储) | `false` | **内部张力**：存储恢复不需要运维动作；`job_errors.py` 的注释写「The `job_unavailable` storage outage never qualifies」，但新规则没有说存储类为什么不算 |
| 3 | `voice_drafts.py` `unavailable`(503 存储) | **`true`** | **跨面冲突**：#2 与 #3 是同一语义（存储不可用），一个 false 一个 true |
| 4 | `voice_drafts.py` `outcome_unknown` / `job_errors.py` `job_outcome_unknown` | 均 `true` | 一致 |
| 5 | `asset_errors.py` `asset_retention_not_configured`（503，「Set a TTL or configure retention.」） | `false` | 一致（运维动作） |
| 6 | `asset_errors.py` `asset_inspection_busy`（503） | `true` | 一致（无带外动作） |
| 7 | `speech_errors.py` `backend_unavailable`/`operation_timeout` | `true` | 一致 |
| 8 | `admin-openapi.yaml` `binding_unavailable` 等 | `true` | 一致（可读性故障，无运维动作） |

`job_errors.py` 自己算出来的三个 `true`（`job_outcome_unknown`、`job_capacity_exhausted`、
`job_admission_contended`）与新规则一致，且 `test_the_retry_rule_follows_the_published_semantics`
把集合钉住了。**冲突集中在 #1–#3 三处。**

## D. 两处行为变更

### D10 voice-draft submit 面：把「引擎未就绪」变成 422，是换了一种错误归因

作者自述：原来除容量外所有准入拒绝借 `voice_draft_unavailable`（可重试 503，文案
「Voice draft storage is unavailable.」）；现在只有 `job_admission_contended` 走 `unavailable`，
其余一律 422 `input_unavailable`。

**实测（`validateVoiceDraftRevision`，注入各码）：**

```text
engine_not_ready        -> 422 {"code":"voice_draft_input_unavailable","type":"invalid_request_error",
                             "message":"Published model or ready input required.","retryable":false}
engine_state_mismatch   -> 422 同上
engine_resource_disabled-> 422 同上
recipe_not_admitted     -> 422 同上
job_request_expired     -> 422 同上
admission_state_invalid -> 422 同上          ← jobs 面这是 500 部署缺陷
workspace_not_configured-> 422 同上
job_admission_contended -> 503 {"code":"voice_draft_unavailable",
                             "message":"Voice draft storage is unavailable.","retryable":true}
unlisted_public_code    -> 422 voice_draft_input_unavailable
```

**结论：是，把「引擎/部署侧原因」换成了「调用方输入错」，比原来并没有更准确。**

理由：

1. **422 的语义是「请求有错」**。`engine_not_ready` 时模型已发布、输入已就绪、请求体完全合法，
   唯一的事实是引擎的 readiness 收据过期。文案「Published model or ready input required.」
   在这五种情况下**都是假的**。
2. **恢复路径被藏掉**。这一面的 4xx 是确定性拒绝（不可重试、也不带 `Retry-After`），
   而 `engine_not_ready` 的真实恢复是「重签收据后同一请求可成功」。把恢复写进 422 的用户输入
   错误里，等于告诉调用方「改你的请求」——而改请求永远不会成功。
3. **它把 500 级缺陷降格成 422**：`admission_state_invalid` 在 jobs 面是
   `500 api_error`（「deployment defect, must not be replayed」），到这里变成 422 输入错误。
   这是把「服务端不变量被破坏」报成用户错，比原来更失真。
4. 只有一处比原来好：`retryable` 从 `true` 变 `false`，不再诱导重试。**但这一点可以在保住
   503 状态的同时做到**（改文案 + `retryable: false`），不必把状态从 5xx 改成 4xx。

**建议**：给这一面加一个引擎侧的 503 线码（例如 `voice_draft_not_ready` 一类），
`retryable: false`，文案点名重签/重注册；把 `admission_state_invalid` 留在 500 类。
如果坚持复用 422，至少要把 `input_unavailable` 的文案改成不归罪于输入的中性描述，
并在契约里写明该 422 也承载引擎侧原因——否则这个码在两个面表达的是相反的责任归属。

附带一条：`job_admission_contended` 仍走 `503 voice_draft_unavailable` +
「Voice draft storage is unavailable.」——**纯并发竞争被说成存储故障**，属于同一类归因错误的
残留（可重试这点是对的）。如采用上面的新码，建议一并给它一个不带存储文案的 503 类。

### D11 workbench `rejectionDisposition` 补 5 行

补的行与分析：

| 行 | 判定 |
| --- | --- |
| `[503, engine_state_mismatch, intent-kept]` | 合理：准入未写行，重注册后同一 key 可成功 |
| `[503, engine_resource_disabled, intent-kept]` | 合理：恢复实例后同一 key 可成功 |
| `[503, recipe_not_admitted, intent-kept]` | 合理：同上 |
| `[422, job_request_expired, unreplayable]` | **分类合理、文案错**（见下） |
| `[500, admission_state_invalid, unknown-outcome]` | 可接受：保守。500 恰在「写入可能已发生」的位置上不可证伪，标 unknown 比标 terminal 安全 |

**`unreplayable` 分支的文案写死了 413 语义。** 该分支（`file-task-session.ts:918-996`）
只有两个用户可见文案，都是：

```text
file-task-session.ts:954  ：请求体超过本部署的接收上限，但该意图此前已有提交尝试…
file-task-session.ts:986  ：请求体超过本部署的接收上限，这次请求被拒绝、原意图作废…
```

`job_request_expired` 进来后，用户会在「准入截止时间已过」时读到「请求体超过接收上限」。
这是**实装层面的错误解释**，不是措辞问题。同时该分支的注释（"answered before the key
lookup"）也只对 413 成立——`job_request_expired` 是在准入事务里、键查之后产生的。
测试只 pin 了 `rejectionDisposition` 的返回值（`file-task-rejection.test.ts:216`），
**没有 pin 文案**，所以这条会静默上线。

**`settlesReplayedIntent` 不加：正确。** 新加的 4 个码都是 503（函数只处理 409/422，加了也不生效）；
`job_request_expired` 是 422 且确实在键查之后产生，但它的处置已经是 `unreplayable`
（清理 unknown 标志与「原键不可重放」是两件事，走 `unreplayable` 通道才是对的），
若同时塞进 `settlesReplayedIntent` 会让两条机制对同一码各自表态。**不加是对的，但需要在
`unreplayable` 分支的注释里写清「本分支现在也承载 422」，否则下一位读者会按 413 理解。**

## E. 例数与 CI

### E12 例数

| 套件 | PR head `e5ef03f6` | 基线（merge-base `6a4d7184` 与最新 main `c9a48963` 实测相同） | 相对基线 | 作者自报 | 一致 |
| --- | --- | --- | --- | --- | --- |
| `tests/ci` | **414** | 404 | +10 | 414 / 404 | ✓ |
| `tests/contract` | **3853** | 3815 | +38 | 3853 / 3815 | ✓ |
| `tests/unit` | **7167 collected / 7164 passed** | 7146 | +21 | 7164 / 7146 | 口径混用（见下） |
| `tests/unit/backend` | **3294** | 3273 | +21 | 3294 / 3273 | ✓ |
| 集成三件套（HTTP admission 9 + engine_registration 5 + production_admission 72） | **86 passed** | — | — | 86 | ✓ |
| 全量 `tests/unit tests/contract tests/ci` | **11431 passed, 3 skipped, 1357 subtests**（270.90s） | — | — | — | 414+7164+3853=11431 ✓ |
| workbench `vitest` | **1070 passed** | — | — | 1070 | ✓ |
| admin `vitest` | **151 passed** | — | — | 151 | ✓ |

口径说明：`tests/unit` 我实测 collected **7167**、其中 3 个 skip（Linux-only `prlimit`），
passed **7164**。作者写的 7164 是 passed，同一行的基线 7146 是 collected，**两个口径混在一行**；
两种口径下的差值都是 +21，所以结论不变，只是数字不能直接对读。

静态检查：`uv run ruff check .` → `All checks passed!`；
`uv run mypy` → `Success: no issues found in 1475 source files`；
`uv run lint-imports` → `Contracts: 9 kept, 0 broken.`；
本报告所在分支 `uv run --no-project ruff format --check .` → `1900 files already formatted`，
exit 0。

### E13 `gh pr checks 839`

**看 run 的 head sha，不看 PR 页面：**

```text
$ gh api /repos/hrygo/SpeechNexus/actions/runs/43701451751 --jq '{head_sha,conclusion}'
{"conclusion":"success","head_branch":"fix/828-readiness-expiry",
 "head_sha":"e5ef03f68655f74ad9175d4dcceb9132d469000e", ...}
$ gh api /repos/hrygo/SpeechNexus/actions/runs/43701451742 --jq '{head_sha,conclusion}'
{"head_sha":"e5ef03f68655f74ad9175d4dcceb9132d469000e","conclusion":"success"}
$ gh pr checks 839 | 状态计数
  27 pass
   1 skipping     ← "Docs: markdown code block formatting"（skipping 是正常条件分支）
```

**27 pass / 1 skipping / 0 fail，head sha 与 `e5ef03f6` 一致。**

## 必须修清单

**M1（阻塞，核心）残余伪装通道必须关死，三处缺一不可。**
A2 的三条通道各自都能单独把「未登记码」还原成 #828 的响应；只修其中一条没有意义。

- (a) `job_errors.py:195` 的 `if code not in _ERRORS: code = "job_unavailable"`
  必须像 `rejection_code` 一样失败（抛错，或映射到一个明确的内部 5xx 码但**文案不得提存储**）。
  并补一条守护：`_ADMISSION_REJECTION_CODES ⊆ _ERRORS`（我实测该守护缺失：把
  `engine_retired` 只加进前者，26 条守护全绿而线上回答 `503 job_unavailable`）。
- (b) `public_jobs.py:1504-1509` 的 `_raise_admission_rejection`：未命中也必须抛错，
  不能静默返回后落 `JobRejectedError("contract") → job_unavailable`。最省事的做法是把
  `_ADMISSION_REJECTION_CODES` 改成从 `_PUBLIC_CODES.values() | CAPACITY_REJECTION_CODES`
  派生，从结构上消灭这个手写集合。
- (c) `voice_drafts.py:427-436` 的 `except (…, KeyError, …)`：`KeyError` 不得再变成
  `503 voice_draft_unavailable` + `retryable: true`。它要么单独处理成 5xx 内部码，
  要么不再被这段兜底捕获（`ValueError`/`TypeError`/`RecursionError` 的归类不必连带 `KeyError`）。

**M2（阻塞）AST 守护要补全清单并处理变量传码。**
当前 6 模块清单只覆盖 13 个 raise 点模块中的 6 个，且只认字面量实参。本卡要求的两个构造
（未扫描模块新 raise 点、变量传码）我都实测 **26 passed 假绿**。至少要：
把 `domain/runtime_intents.py`、`infrastructure/runtime_sql.py`、`owners_sql.py`、
`runtime_observation_sql.py`、`qualification_admission_postgres.py`、`tts_settlement_sql.py`、
`builtin_voice_qualification.py` 纳入；把 `unapproved_runtime_template`、
`gateway_drain_incomplete` 这两个既不在词汇表、又会经 `admission_sql.py:93-94` 透传的码
落定（进词汇表或改成不会被透传）；并把 `test_the_scan_reaches_the_raise_sites` 从
「并集非空」改成「每个模块至少命中 N 个字面量，或用显式白名单声明该模块只做透传」。

**M3（阻塞，改动小）voice-draft 提交面不要用 422 `input_unavailable` 承载引擎侧原因。**
D10 已判定为「换了一种错误归因」；`admission_state_invalid` 从 500 降格成 422 尤其不可接受。
加一个 503 的引擎侧线码（`retryable: false`，文案点名重签/重注册），或至少把
`input_unavailable` 的文案与契约描述改成不归罪于输入。

**M4（阻塞，改动小）workbench `unreplayable` 分支的用户文案与注释。**
`job_request_expired` 现在会走进 413 的文案（`file-task-session.ts:954`、`:986`
「请求体超过本部署的接收上限」），以及 413 专属的注释（"answered before the key lookup"）。
按 `(status, code)` 或按 disposition 的原因区分文案，并补一条 pin 文案的测试
（现在只 pin 了 `rejectionDisposition` 的返回值）。

**M5（阻塞/小）`retryable` 的旧定义与新规则并存，需二选一。**
`contracts/shared/errors.schema.json` 同一字段里仍写「speech Job operations compute this field
from the narrower question 'may an unknown execution outcome be replayed?'」，这句话在返修后已
不成立（`job_capacity_exhausted`/`job_admission_contended` 为 true 与该问题无关），
`contracts/workbench-openapi.yaml:2278-2284` 又重复了一遍。要么删/改这句并以「带外改动」为唯一
判据，要么在通用规则里显式写出例外条款，并同时交代 `execution_cleanup_pending`（false）
与 workbench 同一情形（true）的差别、以及 `job_unavailable`（false）与
`voice_drafts.unavailable`（true）这对同语义反值。

## 建议修（非阻塞）

- **S1** `_ADMISSION_REJECTION_CODES` 与 `_ERRORS` 的关系用派生代替手写（同时消灭 M1(b) 的
  手工登记风险）。
- **S2** `test_the_narrowed_statuses_do_not_advertise_codes_they_cannot_return` 增强为
  「枚举码在 `createJob` 处理器里至少有一个 emit 点」的静态检查（例如从 handler 的
  `job_error("…")` 调用与 `JobAdmissionRejectedError` 分支收集），否则它只证明跨操作状态一致。
- **S3** `engine_resource_disabled`、`recipe_not_admitted`、`job_request_expired` 只有
  映射级证据；照 `test_a_drifted_receipt_identity_is_engine_state_mismatch_not_a_storage_outage`
  的样子补 1–2 条真 PG + 生产 HTTP 往返用例。
- **S4** `ErrorResponse` 结构在 7 处内联复制，是为绕开结构检查器的解析口径；建议在
  `contracts/AGENTS.md` 或 `docs/validation/2026-11-15-engine-readiness-expiry.md` 里把「改
  `ErrorResponse` 必须同步这 7 处」写成明确约束，否则下一次加字段会静默漂移。
- **S5** 结构检查器自述的未证范围包含 `added statuses/media`；本 PR 给 `createJob` 新增了
  500 状态，这一处不在其证明范围内，建议在证据文档里点名。
- **S6** 第一轮的 S1（`remaining_seconds == 0` 但状态 `expired` 的文案边界）本轮未处理，
  不在返修范围，保留跟踪。

## 只能依赖作者陈述的点

| 事项 | 原因 |
| --- | --- |
| 真实引擎的 `/health` 探测与 `--renew-readiness` 对真实端点的效果 | 本机没有真实引擎，按项目约定不用 fake 代替；我只核了事务与 `verified_at_us` 递增路径的代码与用例 |
| `docker compose --profile engine-readiness run --rm …` 的实际容器启动 | 未构建镜像跑容器；只核了 Compose 可解析、服务接线与 `tests/ci` 文本守卫 |
| 宿主机 cron/systemd 定时器本身 | 未安装任何定时任务；文档声称的运维步骤按陈述接受 |
| 远端 CI 六个 Integration 分片的**内部**行为 | 我只核 run 的 `head_sha`、结论与总状态计数，未下载逐分片日志核对断言 |
| `docs/validation/2026-11-15-engine-readiness-expiry.md` 里「rebase 前 3245/370」 | 那是作者的中间时点；我只核了 merge-base `6a4d7184`、最新 main `c9a48963` 与 PR head 三个可复现点 |

## 复现命令

```bash
# 审查 worktree（报告唯一差异）
git worktree add /private/tmp/sn849 -b review/849-readiness-rework origin/main
# 被审代码（只读上下文 + 变异副本）
git worktree add --detach /private/tmp/sn849-run e5ef03f68655f74ad9175d4dcceb9132d469000e
cd /private/tmp/sn849-run && uv sync --frozen --all-packages && pnpm install --frozen-lockfile

# 例数
for d in tests/unit/backend tests/unit tests/contract tests/ci; do
  uv run --no-project python -m pytest "$d" --collect-only -q -p no:cacheprovider | tail -1
done

# 全量
uv run --no-project python -m pytest tests/unit tests/contract tests/ci -q -p no:cacheprovider

# 契约与生成物
make contracts-check && make contracts-drift
CONTRACT_BASE_REF=origin/main make contracts-speech-changes
make contracts-route-conformance

# 一次性 PG / MySQL（用完 docker rm -f）
docker run -d --name sn849-pg -e POSTGRES_USER=speechnexus -e POSTGRES_PASSWORD=speechnexus_test \
  -e POSTGRES_DB=speechnexus_test -p 127.0.0.1:55449:5432 postgres:16.15-alpine
docker run -d --name sn849-mysql -e MYSQL_ROOT_PASSWORD=speechnexus_test \
  -p 127.0.0.1:55450:3306 mysql:8.4.8
SPEECHNEXUS_TEST_DATABASE_URL='postgresql://speechnexus:speechnexus_test@127.0.0.1:55449/speechnexus_test' \
SPEECHNEXUS_TEST_MYSQL_URL='mysql://root:speechnexus_test@127.0.0.1:55450/' \
  uv run --no-project python -m pytest \
    tests/integration/test_public_job_admission_http_pg.py \
    tests/integration/test_engine_registration_pg.py \
    tests/integration/test_production_admission.py -q
```

四条关键变异（都在 `sn849-run` 内，跑完 `git checkout -- <file>`）：

| 编号 | 变异 | 期望 / 实测 |
| --- | --- | --- |
| A2b | `public_job_admission.py` 的 `PublicJobAdmissionRejectedError("workspace_not_configured")` → `"workspace_quota_missing"` | 实测 `503 job_unavailable`「Job storage is unavailable.」 |
| A2c | `admission.py` 的 `expired_code="readiness_expired"` → `"readiness_lapsed"` | 实测 `KeyError` → 裸 `500 Internal Server Error` |
| A3 | 删 `_PUBLIC_CODES` 一行 / 往 `ADMISSION_REJECTION_CODES` 加成员 | 1 failed / 1 failed |
| A4 | 未扫描模块换码 / 扫描模块内变量传码 | 26 passed / 26 passed（假绿） |
| M1(a) | 只往 `_ADMISSION_REJECTION_CODES` 加 `engine_retired` | 守护 26 passed；HTTP `503 job_unavailable` |
| B8 | 删 503 enum 的 `engine_not_ready` | 3 failed |

## 清理

`/private/tmp/sn849-run` 内所有变异已 `git checkout --` 复原，`git status` 为空，
该只读 worktree 在审查结束时 `git worktree remove`；
探针文件（`tests/integration/test_zz_probe_a2.py`、`tests/unit/backend/test_zz_probe_d10.py`、
`tests/contract/test_zz_probe_fallback.py`）已删除；`sn849-pg`、`sn849-mysql` 两个一次性容器
审查结束 `docker rm -f`；本地 `uv sync` 因镜像配置重写过 `uv.lock`，已 `git checkout --` 复原。
未 push main，未合并，未改动任何既有 `docs/reviews/` 文件。
