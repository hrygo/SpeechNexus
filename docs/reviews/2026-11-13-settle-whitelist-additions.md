# PR #717 非作者审查：终态放弃白名单新增三个 422 码

> 过程记录文档：保留审查时间线、变异与探针执行记录。审查对象为 PR #717（`fix/715-settle-more`，head `08c50cad921107b16a018546ee6c18408a0a3d03`，base `7e09b4db`），修 issue #715。
> 审查方式：独立 worktree `/private/tmp/sn720`（`git worktree add /private/tmp/sn720 -b review/720-settle-more 08c50cad`），主检出（停在 `docs/tracker-batch12`）未参与任何读取。实际审查发生在 2026-11-14 08:13–08:30（本地时间，Asia/Shanghai）。
> 环境：系统 node **v26.12.1**、pnpm 12.14.0、uv 0.13.5、CPython 3.14.9。所有变异都在该临时 worktree 内完成并 `git checkout --` 还原，未进入提交，也未 push 变异。
> 性质说明：这是**安全性审查**。白名单（`settlesReplayedIntent`）的安全性完全建立在「该码只在原键重放检查之后产生」这一证明上，加错一个码就等于给「清掉未知态 → 重复执行」开口子。

## 结论

**可以合并。** 三个码逐个对着后端实现核过，**每一个产生点都确实落在「原键已查过」之后**，含 `asset_not_ready` 的**两个不同产生点**（应用层 `_require_inputs` 与准入事务内的 store 拒绝）——后者靠事务内 `pg_advisory_xact_lock` + `_existing_by_key` 而不是 `submit` 的 except 重查来保证，机制不同但结论成立。作者「没有发现不在原键检查之后的码」这句**成立**（在 asr/tts create 这条本客户端实际使用的路径上）。三个反例独立复现，数字与断言位置一致；既有未知保护用例一条未动、全部仍绿；重载是真重载（新 session 从持久记录重建，无 `restore()` 也已是结算态）；用户可见语义与既有 `409 idempotency_conflict` 结算路径逐字同源。改动仅 2 文件 +58/−0，`uv.lock` 干净。

不阻塞的改进项 3 条（见「必须修清单」后的建议修）：注释里准入 store 抛点的行号引用指到了应用层映射而非真正的 `raise` 位置；两段证明链（except 重查 vs 事务内 key 锁）在注释里没有分开写；PR 正文把 CI 红说成「`commit_unknown` 失败」，实际先失败的是 `assert sqlstate == "23503"`。

| # | 审查项 | 判定 | 一句话证据 |
| --- | --- | --- | --- |
| 1 | 三码逐码对后端核产生点 | **成立** | model 4 处 / profile 1 处 / asset 2 条路径，全部在 `submit` 前置重放（`:607-612`）之后；后端探针实测「同键已有 Job 时这三条事实**一次都没读**」 |
| 2 | 有没有「不该加却加了」 | **成立（未发现）** | create 路由在这三码之前只有 `invalid_job_request`/`unsupported_parameter`；三码的其它产生点都在别的端点（retry / voice-draft revision / speech / transcriptions / profile admin），本客户端那三条路径都不发 |
| 3 | 反例独立性 | **成立** | 三个码各自移出白名单 → 三次都是 `2 failed \| 48 passed (50)`，红在判定表行 + 会话用例的 `unknown:false/rejected:true/canReplay:false` 断言 |
| 4 | 有没有顺手放宽未知保护 | **成立（未放宽）** | diff 为纯新增 `+58/−0`；`409 execution_cleanup_pending`(create) 仍 `unknown:true`，401/403/404/405/400/413/`unsupported_parameter` 仍在名单外，全绿 |
| 5 | 「重载仍结算」是否真验 | **成立** | 探针：持久记录里有 `"settled":"terminal"`；**新 session 只靠构造函数**（不调 `restore()`、零网络）就得出 `unknown:false/rejected:true/canReplay:false` |
| 6 | 用户可见语义 | **成立** | 与 `409 idempotency_conflict` 同一分支：notice = 后端 message、`rejected:true`、`canFinishTask:true`，面板出「准备新任务」→「放弃这次意图并新建」 |
| 7 | 边界 | **成立** | 仅 `web/workbench/src/file-workflow/` 两文件；`uv.lock` diff 为空 |
| 8 | CI 归因 | **成立（描述略粗）** | 同一 sha 在 main 自己的 CI 上是同一个 `test_database_failure_rolls_back_header_revision_and_receipt` 红；修复 PR #720（#718）只碰 backend workbench；当前 main（`bca43401`、`71054af2`）绿 |

## 时间线

1. 08:13 `git fetch`；`git worktree add /private/tmp/sn720 -b review/720-settle-more 08c50cad`；确认 `merge-base = 7e09b4db`、diff 只有 2 文件 `+58/−0`、`uv.lock` 不在 diff 内。
2. 08:14 `pnpm install --frozen-lockfile`（269 包，439ms，lockfile 供应链策略通过）。
3. 08:16 `pnpm test` → **62 files / 817 tests 全绿**。
4. 08:17 反例三轮（逐码移出白名单，每轮后 `git checkout --`）→ 三轮都是 `2 failed | 48 passed (50)`。
5. 08:18 基线：临时把两条被改文件取回 base 版本 → `811 passed`；确认 `+6`。
6. 08:19 `typecheck` exit 0、`lint`「ESLint: passed」、`format:check`「All matched files use Prettier code style!」。
7. 08:20 `uv run ruff format --check .` → 1827 files already formatted；`uv.lock` 无改动。
8. 08:21 后端 emission-order 探针（临时文件，跑完删除）；前端 persisted-reload 探针（临时文件，跑完删除）。
9. 08:24 CI 归因核查（`gh pr view 717` / `gh run view` / `gh pr view 720`）。
10. 08:30 写本报告。

## 1. 三码逐码：是否真的在原键检查之后（本卡核心）

### 1.0 三条 create 路径的共同骨架（先看骨架，再看每码落点）

`PublicJobs.submit`（`backend/src/speechnexus_backend/speech/application/public_jobs.py:578`）对本客户端（asr/tts create）的顺序是：

| 行 | 动作 | 失败能否在「原键已查」之前抛 |
| --- | --- | --- |
| `:585-599` | `kind` 白名单、`workspace_id` 类型 | 是（→ `job_kind_not_supported` / `workspace_not_found` / `workspace_disabled`） |
| `:604-606` | `_canonical_intent` + `canonical_digest` + `idempotency_digest` | 是（→ `invalid_job_request`） |
| `:607-612` | **前置原键重放** `_find_original`（命中即返回 Job） | — |
| `:614-623` | `_submit_new` → `_prepare_job`（公开事实解析 + 准入） | 否（已过原键检查） |
| `:624-635` | `except` 元组**包含**三个错误类 | — |
| `:641-646` | **失败后再查一次原键**，命中即返回 Job | — |
| `:647` | `raise`（此时两次 `find_by_key` 都落空） | — |

`submit_revision`（`:649`，voice-draft revision 路由）与 `retry`（`:1090`）是同一骨架：各自先 `_find_original`（`:679-684`、`:1134-1142`），再调 `_prepare_job`，再 `except` 重查（`:699-717`、`:1153-1170`）。本客户端只走 `submit`；另外两条不是本客户端的 create 路径，但它们共同说明一件事：**`_prepare_job` 的全部三个调用者都在自己的原键检查之后**。

### 1.1 `422 model_not_published`：成立（4 个产生点全在 `_prepare_job` 内）

| 行 | 产生条件 | 位置判定 |
| --- | --- | --- |
| `:783` | `model` 不是字符串 | `_prepare_job`（`:741`），由 `_submit_new`（`:614`）调用 → 在原键重放 `:607` 之后 |
| `:801` | `latest_binding(model)` 为 `None`（无已发布绑定） | 同上 |
| `:805` | 绑定缺所需 task（`asr_file`/`tts_file`） | 同上 |
| `:837` | `catalog_revision() < 1`（无目录版本） | 同上（`_require_inputs` 之后、构造 Job 之前） |

再无其它产生点：`grep ModelNotPublishedError` 全仓命中只有这 4 处 `raise` + `submit`/`submit_revision`/`retry` 的 `except` 元组 + `__all__`。HTTP 层 `interfaces/http/jobs.py:480`（create）与 `:645`（retry）只做码映射。

### 1.2 `422 profile_not_published`：成立（1 个产生点）

`:822`：`profile_publication is None` → `raise ProfileNotPublishedError`，位于 `_prepare_job` 内、`_require_inputs`（`:833`）之前。同码在别的端点也有产生点（`/v1/audio/speech` `speech.py:397`、`/v1/nexus/transcriptions` `transcriptions.py:468`、profile 发布管理 `profile_publications.py:128`），但都不是本客户端的 create 路由。`submit` 的 `except` 重查（`:641-646`）对它生效。

### 1.3 `422 asset_not_ready`：成立（**两个**不同产生点，两条分别核）

这是本卡特别要求分开核的地方。

**(a) 应用层 `_require_inputs`（`:833` → `:1379-1431`）**

- `_require_inputs` 是 `staticmethod`，全仓只有一个调用点：`:833`，在 `_prepare_job` 内。
- raise 点 4 处：`:1382`（`input` 不是 dict）、`:1415`（voice_clone 参考音频）、`:1427`（asr/tts 的 asset id 不是字符串）、`:1431`（asset 读回不是 `("ready", expected)`）；其中 asr/tts 可达的是 `:1427`/`:1431`。
- 因此它只在 `_find_original`（`:607`）之后、且 `submit` 的 except 会在 `:641` **再查一次原键**，查不到才 `raise`（`:647`）。**满足「查不到原键 Job 才抛」。**

**(b) 准入事务内的 store 拒绝（这是卡里点名的「最后一个」）**

- 真正的 raise 点在 `infrastructure/public_job_admission.py:424`：`_audio`（`:409`）读 `speech_assets`，`asset_state != "ready" or purpose != "asr_input"` → `PublicJobAdmissionRejectedError("asset_not_ready")`。
- 对 asr/tts 可达的**只有这一处**：`_audio` 的唯一调用点是 `:629`，在 `job.kind == "asr"` 分支内（tts 走 `text_asset_id` 时是 `job_text_asset_unsupported`，不是本码）。`:882-914` 的另外 5 处 `asset_not_ready` 在 `_voice_reference`（`:877`）里，只服务 `voice_validation`（调用点 `:778`），本客户端不发。
- 它确实更晚：`build_public_job_admission`（`:545`）由 `infrastructure/public_jobs_postgres.py:569` 调用，在 `create`（`:513`）的**同一事务内**、且在下面这次 key 检查**之后**：
  - `:530-536`：`if job.idempotency_key_digest is not None:` → `_lock_submission_key`（`:1150`，`pg_advisory_xact_lock(hashtextextended('speechnexus:public-job-key:' || current_schema() || ':' || key), 0)`）→ `_existing_by_key`（`:1410`）→ 命中即 `_replay_or_conflict` **返回**，不会走到准入；`:539-542`（带 `expected_binding`）、`:559-562`（无 pin）又各查一次。
  - 结论：**同一 key 的前序 writer 被事务级 advisory lock 串行化**，拿到锁后读到已提交的 Job 就重放，绝不进入 `build_public_job_admission`。所以这条路径也**满足「查不到原键 Job 才抛」**，且**不依赖** `submit` 的 except 重查——事实上 `JobAdmissionRejectedError` **不在** `submit` 的 except 元组里（`:624-635`），它是直接穿出 `submit` 到 `interfaces/http/jobs.py:504` 的 `except JobAdmissionRejectedError` 才被映射成 422 的。保障来自事务内那把 key 锁。

> 补充：`asset_not_ready` 的码字符串在本仓库还有一处不同语义的产生点——voice-draft 路由把 `AssetNotReadyError` 映射成 `input_unavailable`（`interfaces/http/voice_drafts.py:345-359`），不是 `asset_not_ready`，因此不构成同名码的第二来源。

### 1.4 探针：把上面的「静态阅读」变成实测

临时后端探针（`tests/unit/backend/__review_probe_715.py`，跑完即删）用真实 `PublicJobs` + 假 `Facts`/`Store`，每个码两个场景：

```python
"""Review probe (#717 / #715): is each 422 emitted only after the original key is checked?"""

from __future__ import annotations

from typing import Any

import pytest

from speechnexus_backend.speech.application.public_jobs import (
    AssetNotReadyError,
    BindingFact,
    ModelNotPublishedError,
    ProfileNotPublishedError,
    PublicJobs,
    WorkspaceFact,
    _canonical_intent,
)
from speechnexus_backend.speech.domain.public_jobs import (
    JobCreateResult,
    JobSubmission,
    PublicJob,
    canonical_digest,
)
from speechnexus_backend.speech.domain.publications import Publication

BINDING_TTS = Publication("binding", "tts-quality", 2, "binding-rev-2", 1_000)
BINDING_ASR = Publication("binding", "asr-quality", 1, "binding-rev-1", 1_000)
VOICE = Publication("voice", "voice-vivian", 4, "voice-rev-4", 1_000)
NOW = 1_700_000_000_000_000


class ProbeFacts:
    """Facts whose publication/asset read produces the code under test."""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.reads: list[str] = []

    def workspace(self, workspace_id: str) -> WorkspaceFact:
        return WorkspaceFact(7, True)

    def latest_binding(self, model_id: str) -> BindingFact | None:
        self.reads.append("latest_binding")
        if self.mode == "model_not_published":
            return None
        if model_id == "asr-quality":
            return BindingFact(BINDING_ASR, {"tasks": ["asr_file"]})
        return BindingFact(BINDING_TTS, {"tasks": ["tts_file"]})

    def binding_revision(self, binding_id: str, revision: int) -> BindingFact | None:
        return None

    def publication(self, kind: str, object_id: str, revision: int) -> Publication | None:
        self.reads.append(f"publication({kind})")
        if kind == "profile":
            return None
        return VOICE

    def pronunciation_exists(self, object_id: str, revision: int) -> bool:
        return False

    def asset(self, workspace_id: str, asset_id: str) -> tuple[str, str] | None:
        self.reads.append("asset")
        if self.mode == "asset_not_ready":
            return ("inspecting", "asr_input")
        return ("ready", "asr_input")

    def voice_draft_revision(self, draft_id: str, revision: str) -> None:
        return None

    def catalog_revision(self) -> int:
        return 31

    def now_us(self) -> int:
        return NOW


class ProbeStore:
    """Records key lookups; refuses to write (both scenarios stop before create)."""

    def __init__(self, seeded: PublicJob | None = None) -> None:
        self.find_calls: list[str] = []
        self.create_calls = 0
        self.seeded = seeded

    def find_by_key(
        self,
        workspace_id: str,
        key_digest: str,
        request_digest: str,
        retry_of_job_id: str | None,
    ) -> JobCreateResult | None:
        self.find_calls.append(key_digest)
        if self.seeded is not None:
            return JobCreateResult("replayed", self.seeded)
        return None

    def create(self, job: Any) -> JobCreateResult:
        self.create_calls += 1
        raise AssertionError("probe: the refusal scenario must not write a Job")

    def get(self, job_id: str) -> PublicJob | None:
        return None


def _seeded_job(kind: str) -> PublicJob:
    submission = JobSubmission(
        request_id="req-original",
        client_reference=None,
        effective_configuration_json="{}",
        execution_revision=1,
        progress_unit="audio_ms",
        progress_total=None,
        submitted_at_us=NOW,
    )
    return PublicJob(
        "job-original",
        "workspace-default",
        kind,  # type: ignore[arg-type]
        "tts-quality",
        "pending",
        1,
        None,
        None,
        NOW,
        NOW,
        submission,
    )


TTS: dict[str, Any] = {
    "kind": "tts",
    "workspace_id": "workspace-default",
    "model": "tts-quality",
    "input": {"text": "synthetic hello"},
    "voice": {"id": "voice-vivian", "revision": "4"},
}
TTS_PROFILE: dict[str, Any] = dict(TTS, profile={"id": "profile-x", "revision": "2"})
ASR: dict[str, Any] = {
    "kind": "asr",
    "workspace_id": "workspace-default",
    "model": "asr-quality",
    "input": {"audio_asset_id": "asset-demo"},
}

CASES: dict[str, tuple[type[Exception], dict[str, Any]]] = {
    "model_not_published": (ModelNotPublishedError, TTS),
    "profile_not_published": (ProfileNotPublishedError, TTS_PROFILE),
    "asset_not_ready": (AssetNotReadyError, ASR),
}


@pytest.mark.parametrize("code", sorted(CASES))
def test_probe_715(code: str) -> None:
    error, payload = CASES[code]
    kind = payload["kind"]
    canonical_digest(_canonical_intent(payload, kind))

    # (B) No Job under the key: the refusal is raised, and the key was read before the facts.
    facts_b, store_b = ProbeFacts(code), ProbeStore()
    with pytest.raises(error):
        PublicJobs(facts_b, store_b, now_us=lambda: NOW).submit(
            payload, request_id="req-create", idempotency_key="idem-715-probe-0001"
        )
    print(f"\n[{code}] (B) refusal: find_by_key={len(store_b.find_calls)} reads={facts_b.reads}")
    assert len(store_b.find_calls) >= 2, "key must be read before and after the failing facts"
    assert store_b.create_calls == 0

    # (A) A Job already committed under the key: the code must be unreachable.
    facts_a = ProbeFacts(code)
    store_a = ProbeStore(seeded=_seeded_job(kind))
    job = PublicJobs(facts_a, store_a, now_us=lambda: NOW).submit(
        payload, request_id="req-create", idempotency_key="idem-715-probe-0001"
    )
    print(f"[{code}] (A) replayed job={job.job_id} reads={facts_a.reads}")
    assert job.job_id == "job-original"
    assert facts_a.reads == [], "no mutable fact may be read once the key has a Job"
```

实跑输出（`uv run pytest tests/unit/backend/__review_probe_715.py -q -s`）：

```text
[asset_not_ready] (B) refusal: find_by_key=2 reads=['latest_binding', 'asset']
[asset_not_ready] (A) replayed job=job-original reads=[]
[model_not_published] (B) refusal: find_by_key=2 reads=['latest_binding']
[model_not_published] (A) replayed job=job-original reads=[]
[profile_not_published] (B) refusal: find_by_key=2 reads=['latest_binding', 'publication(voice)', 'publication(profile)']
[profile_not_published] (A) replayed job=job-original reads=[]
3 passed in 0.04s
```

两条结论由实测给出，而不是读注释：

- **(B) 拒绝路径**：`find_by_key` 被调用 **2 次**（前置 + 失败后重查），之后才抛码。
- **(A) 同键已有 Job**：三条可变事实（`latest_binding` / `publication` / `asset`）**一次都没被读**，`submit` 直接返回原 Job。这就是「查不到原键 Job 才抛」的机器证明——只要原键有 Job，这个码不可能出现在响应里。

> 准入事务那条路径（1.3(b)）没有跑运行时探针，因为它需要真实 PostgreSQL 与准入角色；该条的证据是静态的：`public_jobs_postgres.py:530-536`（`_lock_submission_key` → `_existing_by_key` → 命中即重放）在 `:569`（`build_public_job_admission`）之前，锁是 `pg_advisory_xact_lock`（`:1155`）。本报告将其标注为「静态核对，未运行时复现」。

## 2. 「不该加却加了」：未发现

作者主张「没有发现不在原键检查之后的码」，核实**成立**（在 asr/tts create 这条本客户端唯一使用的路径上）：

- create 路由在 `PublicJobs.submit` 之前只有两类拒绝：`interfaces/http/jobs.py:442-461` 的 body 解析/契约校验 → `invalid_job_request` / `unsupported_parameter`。三码都不在这两层产生。
- `submit` 内部 `:585-606` 的 kind/workspace 校验也不产生这三码。
- 三码的**其它产生端**逐个排除：

| 同码产生端 | 是否可能被本客户端观察到 |
| --- | --- |
| `retry`（`jobs.py:645-656`） | 否：客户端从不调用 `POST /v1/nexus/jobs/{id}/retries`（`web/workbench/src/file-workflow/` 内除 `create`/`cancel`/asset 读外无 retries 调用） |
| `submit_revision`（voice-draft revision，`public_jobs.py:688`） | 否：客户端不写 voice draft |
| `/v1/audio/speech`（`speech.py:397`）、`/v1/nexus/transcriptions`（`transcriptions.py:468`） | 否：不同客户端/端点 |
| profile 发布管理（`profile_publications.py:128`、`profile_publications_sql.py:176/248`） | 否：admin 端点，且该路由的 409/404 语义不同 |
| voice-draft 路由的 `AssetNotReadyError` | 码被映射成 `input_unavailable`，不是 `asset_not_ready` |
| 准入 store（`public_job_admission.py:424`） | 相关，但见 1.3(b)：在事务内 key 锁之后 |

所以「按码字符串判定」在本客户端的边界内成立。**边界条件**（不是缺陷，但应知道）：同一批码字符串确实还有别的产生点，`settlesReplayedIntent` 只看字符串。今天安全是因为它只在 `operation === "create"` 且持久记录 `pending === "create"` 时参与判定（`file-task-session.ts:566-572`），而 `pending: "create"` 只由 `#submit` 写入（`:786`）。若将来客户端接入 retry 或 voice-draft 修订，必须按端点重新核一遍，不能沿用这份名单。

另有一条**共同前提**（既有成员码同样依赖，非本 PR 新增风险）：白名单的证据链默认 `Idempotency-Key` 确实抵达后端。`submit` 在 `key_digest is None` 时既不做前置重放也不做失败重查（`:607`、`:641`），契约也明说「Keyless requests have no such replay guarantee」（`contracts/speech-openapi.yaml:852-853`）。本客户端始终发送 `#createKey`（`crypto.randomUUID()`，`speech-file-client.ts:96-99` 写 `Idempotency-Key`），故不构成新暴露面。

## 3. 反例独立复现（三个都做，不止两个）

方法：在临时 worktree 内把单个码从白名单删掉 → 只跑 `src/file-workflow/file-task-rejection.test.ts` → `git checkout --` 还原（每轮 `git status` 复查为空）。

```text
$ git checkout -- ... && python3 -c '删掉 error.code === "model_not_published" ||'
 FAIL  sr.../file-task-rejection.test.ts > replayed-intent settlement predicate > settles only 422 model_not_published after the key check: true
 FAIL  sr.../file-task-rejection.test.ts > submission rejection classes > settles a 422 model_not_published refusal as an abandonable spent intent and keeps it settled after reload
- Expected: { canReplay: false, rejected: true, unknown: false }
+ Received: { canReplay: true,              unknown: true }
 Tests  2 failed | 48 passed (50)

$ （还原）删掉 error.code === "asset_not_ready" ||
 × settles only 422 asset_not_ready after the key check: true
 Tests  2 failed | 48 passed (50)

$ （还原）删掉 error.code === "profile_not_published" ||
 Tests  2 failed | 48 passed (50)
```

- 三轮数字与作者报的 `2 failed | 48 passed` 逐字一致。
- 红的位置正是预期断言：判定表行（`settlesReplayedIntent` 返回 false）+ 会话用例的「已作废」断言（`expect(snapshot).toMatchObject({ unknown: false, rejected: true, canReplay: false })`，`file-task-rejection.test.ts:422`），实际回到 `unknown:true / canReplay:true`。
- 这说明三个码**真的被这条路径覆盖**，不是被别的分支兜住。

## 4. 有没有顺手放宽未知保护：没有

- `git diff --numstat 7e09b4db..08c50cad` → `43 0` 测试、`15 0` 源码，**纯新增 58 行、0 删除**。既有断言一行未动（不是「看起来没动」，是字面 0 删除行）。
- 逐条复查仍成立的既有保护（全部在本 head 上全绿）：
  - `409 execution_cleanup_pending`（create）：`rejectionDisposition` 在 `:103-104` 把 create 的它归为 `unknown-outcome`（`file-task-session.ts` 的 `operation !== "cancel" && status === 409 && code === "execution_cleanup_pending"`），用例 `keeps the intent unknown when a create attempt is refused with 409 execution_cleanup_pending` 断言 `unknown:true, canReplay:true`、`recovery.error.code` 保留。
  - `401 invalid_credentials` / `403 forbidden` / `404 job_not_found` / `405 method_not_allowed` / `400 invalid_job_request` / `413 request_too_large` / `422 unsupported_parameter` / `422 workspace_not_found` / `409 operation_in_progress` / `409 voice_draft_outcome_unknown`：判定表全部仍为 `false`。
  - 『丢失应答后重放被 key 前拒绝』两条（页内 + 重载后）仍 `unknown:true` 且无 `settled`。
  - 『带 `nexus.operation_id` 的拒绝不结算』仍成立（`settles` 的 `!contractError.detail.error.nexus?.operation_id`）。
- 基线独立实测：把两条被改文件取回 base 版本 → **811 passed**；本 head → **817 passed**（+6 = 3 条判定表行 + 3 条会话用例），与作者数字一致。

## 5. 「重载仍结算」是真重载

用例写法是 `first.dispose()` → `const restored = f.make()` → `await restored.restore()`，其中 `f.make()` 每次都是 `new FileTaskSession(client, new SessionTaskJournal(storage, "task"))`，构造函数从 `journal.load()`（`storage.getItem`）读持久记录（`file-task-session.ts:234-303`，其中 `:285-299` 由 `settled` 重建 `unknown/rejected/canReplay`）。

我的探针把这条压到最强：**新 session 连 `restore()` 都不调**，只看构造函数产出的快照：

```text
model_not_published persisted: {"model":"asr-quality","assetId":"asset-demo","workspaceId":"ws-default",
  "createKey":"3e9c…","cancelKey":"acd6…","jobId":null,"pending":"create","operationId":null,
  "cursor":null,"version":1,"settled":"terminal"}
model_not_published reload snapshot: {"unknown":false,"rejected":true,"canReplay":false,
  "notice":"已找到原任务身份；该意图已被协议确定性拒绝并作废。"}
（profile_not_published / asset_not_ready 输出相同形状）
3 passed
```

探针同时断言「构造函数重载期间网络读次数不变」（0 次新请求），并断言 `canFinishTask` 为 true。即：**结算结论确实落在持久记录里**（`settled:"terminal"`），不是内存快照的自说自话；`rejected/unknown/canReplay` 三个字段都由 `settled` 重建（`:285-300`）。`restore()`→`refresh()` 对「有 record、无 jobId、无 asset」的记录不发起读（`:861-907` 的分支都不命中），所以用例里 `await restored.restore()` 不会把结算态冲掉，也不会掩盖问题。

## 6. 用户可见语义：与 `409 idempotency_conflict` 同源

同一个分支（`file-task-session.ts:573-604`）：`settles=true` → `#persist({settled:"terminal"})` → `notice: contractError.message`、`rejected:true`、`unknown:false`、`canReplay:false`、`recovery:undefined`。后端 message 是确定性陈述（`job_errors.py:50/77/87`：「The model is not published.」/「The profile revision is not published.」/「The input asset is not ready.」），不是「结果未知」。

放弃出口由 `canFinishTask`（`file-task-policy.ts:3-19`，`rejected === true && !job`）驱动，面板在 `FileTaskPanel.tsx:249-286` 显示「准备新任务」→ 确认区文案「原提交已被协议确定性拒绝，这次意图已作废。结束本页记录并重新开始？新任务会使用新的幂等键，不会删除服务端数据。」+ 按钮「放弃这次意图并新建」。这与 `409 idempotency_conflict` 路径完全一致（同一 `state.rejected` 分支），也与既有 `422 job_text_asset_unsupported` 一致。**结论：语义正确，无「结果未知」残留。**

## 7. 边界

- 改动文件：`web/workbench/src/file-workflow/file-task-session.ts`（+15）、`web/workbench/src/file-workflow/file-task-rejection.test.ts`（+43）。没有第三种文件。
- `uv.lock`：`git diff 7e09b4db..08c50cad -- uv.lock` 为空；审查期间 worktree 内 `git status --short uv.lock` 也一直为空。
- 未触碰后端、契约、生成物。作者只加前端判定与其单测，后端行号依据是静态阅读——这点作者在 PR 正文里自述，属实。

## 8. CI 归因

- PR #717 的 CI（run 43528446840）在该 head 上的结论：`Web: lint, types, tests, build and accessibility` **SUCCESS**；`Python: lint, types and contract compatibility` **SUCCESS**；`Python: tests, coverage and package build` **FAILURE**。
- 失败面（`--log-failed` 实测）：`tests/integration/test_workbench_versions_postgres.py::test_database_failure_rolls_back_header_revision_and_receipt` → `AssertionError: assert None == '23503'`（`:367`），随后 `Python coverage rejected: ... A Python probe failed`。
- 归因**成立**：
  - 同一 sha `7e09b4db` 在 **main 自己的 CI**（run 43527451664）上红的是同一个 job、同一个用例 → 不是本 PR 引入（PR 只改 2 个前端 TS 文件，与该后端集成用例无因果关系）。
  - 修复 PR 是 **#720**（`fix/718-commit-classification`，open，尚未合并）：只改 `backend/.../workbench/application/version_ports.py`、`infrastructure/postgres_versions.py` 与新增单测，正文给出的确定性复现正是 `sqlstate=None/commit_unknown=True` —— 与本 CI 红完全对得上。
  - **当前 main CI 已恢复**：`bca43401`（#719 docs）与 `71054af2`（#710 test）两次 main run 均 success。但要注意这是**偶发**失败（#720 正文写「CI 上偶发」），基线缺陷仍在 main 上，要等 #720 合并才真正消掉。
- 描述精度（不阻塞）：作者说「因 `commit_unknown` 失败而红」，实际**先**失败的是 `assert error.value.sqlstate == "23503"`（下一行才是 `assert not error.value.commit_unknown`）。同一缺陷的两个断言，归因方向没错，只是说了症状的另一半。

## 必须修清单

**必须修：0 条（无阻塞项）。** 逐条核对后，白名单新增成员的安全性、反例覆盖、既有保护、重载语义、用户语义、边界与 CI 归因均成立，未发现「加错码」或「放宽未知保护」。

### 建议修（不阻塞合并，但建议在同一 PR 或紧随的文档改动里收掉）

1. **P2 注释行号指错文件**：注释把准入 store 的 `asset_not_ready` 产生点引到 `public_jobs.py:1468-1488`，但那一段是应用层的 `_ADMISSION_REJECTION_CODES`（`:1468`）与 `_raise_admission_rejection`（`:1484`）——是**识别/再抛**，不是 store 的 `raise`。真正的抛点在 `infrastructure/public_job_admission.py:424`（asr 的 `_audio`；`:882-914` 属 voice 参考音频）。建议改成 `public_job_admission.py:424`（可加注 `public_jobs.py:1468-1488` 是它的应用层再抛）。理由：这段注释就是将来复核这份白名单的唯一证据链，指错文件会让人以为「store 里没有 raise」。
2. **P2 两段证明链应分开写**：注释现在把「except 重查」当作通用保障，但准入 store 的 `asset_not_ready` 走的是另一条：`JobAdmissionRejectedError` **不在** `submit` 的 except 元组里（`:624-635`），它的保障是 `PublicJobsPostgres.create` 事务内的 `pg_advisory_xact_lock` + `_existing_by_key`（`public_jobs_postgres.py:530-536`、`:1150-1158`）。建议在注释里点明「应用层三处由 `submit:641-646` 重查覆盖；准入 store 那处由事务内 key 锁覆盖」。理由：机制不同，未来若有人改动 `submit` 的 except 元组或 `create` 的加锁顺序，需要知道哪条路径会失守。
3. **P3 PR 正文的 CI 症状描述**：把「`commit_unknown` 失败」改成「`test_database_failure_rolls_back_header_revision_and_receipt` 的 `assert sqlstate == "23503"` 失败（同用例的 `commit_unknown` 是相邻断言）」，与日志一致。

## 新绿

- `#707` 的「确定性拒绝后无出口」在这三个码上消除：结算后 `unknown:false / rejected:true / canReplay:false`，`canFinishTask:true` 且面板出「放弃这次意图并新建」；重载后仍是结算态（不是复活成 unknown）。
- 三个码各自可从白名单剥除 → 对应用例变红（覆盖性是实的，不是搭便车）。
- 既有 16 组谓词表 + 50 例 rejection 用例全绿，未知保护无回退；全套 62 files / 817 tests 绿（基线 811，+6）。

## 只能依赖作者陈述的点 / 未验证项

1. **准入事务内 `asset_not_ready` 的运行时顺序**：只做了静态核对（读到 `pg_advisory_xact_lock` + `_existing_by_key` 先于 `build_public_job_admission`），没有在真实 PostgreSQL 上跑并发探针（需要准入角色与隔离库）。结论依赖对 `pg_advisory_xact_lock` 事务级语义的常规理解。
2. **真实后端 HTTP 端到端复现**：我没有起后端服务实打一次 422（作者也没做，PR 正文如实标注了）。三码的「产生点在原键之后」是「实现静态阅读 + 后端探针（假 store/facts 走真实 `PublicJobs`）」两层证据，不是真实 HTTP。
3. **作者自述的基线 811**：我独立实测得到 811（base 版本）/ 817（head），与作者一致；作者报的 `2 failed | 48 passed` 反例数字我也独立复现，故不存在只能依赖其陈述的数字。
4. **PR #720 与 CI 失败的因果**：依据是 PR #720 的改动范围与失败日志吻合 + 同一 sha 在 main 上同样红；我没有在本地复跑该 PG 集成用例（需要 PostgreSQL 与故障注入函数权限）。
