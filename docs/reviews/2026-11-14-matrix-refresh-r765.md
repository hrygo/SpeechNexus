# 2026-11-14 交付矩阵刷新（PR #765）非作者审查

审查对象：PR #765 `docs/760-matrix-refresh`，head `cd1fdac699ce565a226f0ce490af01d9ec696f4f`（单提交，父提交 `26fd8b39`）。
交付矩阵与 issue 快照刷新：三文件 `docs/plans/delivery-issues.json`、`docs/plans/delivery-tracking.md`、`docs/plans/github-issues-snapshot.json`，`40 insertions(+), 40 deletions(-)`。

审查性质：非作者、只读复算。不修改矩阵/manifest/快照，不运行会写回交付物的刷新命令（渲染只在临时检出内进行，用于比对）。

报告落点说明：`docs/reviews/2026-11-14-matrix-refresh.md` 已被上一轮审查（PR #726 / 审查 PR #727，`404bfecd`）占用，本轮不覆盖既有评审记录，改记于本文件（`-r765` 后缀区分）。

## 审查基线

独立 worktree，基于 `origin/main`：

```console
$ git fetch
$ git worktree add /private/tmp/sn766 -b review/766-matrix-refresh origin/main
HEAD is now at fe165d8a docs: 记录渲染拒绝词表与错误归因的非作者审查 (#750) (#764)

$ git rev-parse HEAD
fe165d8a2f233ce20dee7444105268140d46f070
$ git merge-base --is-ancestor origin/main HEAD && echo ok
ok
$ git diff origin/main...HEAD --stat      # 建分支时为空
```

被审内容全部用 `git show cd1fdac6:<path>` 读取，不读作者工作区。

`origin/main` 在 PR 分叉后前进一步（`26fd8b39` → `fe165d8a`），该提交只新增 `docs/reviews/2026-11-14-render-refusal-vocabulary.md`，未触及 `docs/plans/`，因此 PR 的三点 diff 与本审查基线兼容：

```console
$ git diff origin/main...cd1fdac6 --stat
 docs/plans/delivery-issues.json        | 26 +++++++++++++-------------
 docs/plans/delivery-tracking.md        | 22 +++++++++++-----------
 docs/plans/github-issues-snapshot.json | 32 ++++++++++++++++----------------
 3 files changed, 40 insertions(+), 40 deletions(-)
```

渲染门禁与基线 `check-doc-refs` 在另外两个临时检出中执行：`/private/tmp/sn766-gate`（`--detach cd1fdac6`）与 `/private/tmp/sn766-base`（`--detach 26fd8b39`），审查结束后移除。

## 结论

**通过，无阻断项。** 十一条清单逐条成立；`F-JOB` 那处事实修正**正确**，与 `origin/main` 当前代码、`#569` 关闭回执、`gh` 实时状态三方一致；**未引入新的假事实**。渲染门禁本地幂等，`check-doc-refs` 改前改后同为 3 条既有 MISSING，范围仅三文件。

必改项：无。建议改动 3 条措辞（见「必须修清单」），均不影响矩阵事实成立。

## 十一条逐条判定

| # | 检查项 | 判定 |
| --- | --- | --- |
| 1 | 快照/manifest/矩阵自洽，重渲染幂等 | 通过 |
| 2 | manifest 每个 issue 引用与实时状态对账 | 通过（61 条全对，0 反例） |
| 3 | 4 个新 open 项已登记且残留写进需求行 | 通过 |
| 4 | `F-JOB` 改后句子与当前代码一致 | 通过（本卡核心） |
| 5 | 改后句子未引入新假事实 | 通过 |
| 6 | `blob/<sha>/<path>` 证据链接无失效 | 通过（全量核，非抽样） |
| 7 | 本轮新增文字不写新行号 | 通过 |
| 8 | `check-doc-refs.py` 改前后 MISSING 对比 | 通过（另附一条归因更正） |
| 9 | 离线渲染门禁连续两次 MD5 一致 | 通过 |
| 10 | 范围只三文件、`uv.lock` 干净、未碰代码/契约/CI | 通过 |
| 11 | 作者对三处输入事实的纠正 | 三条全部正确 |

### 1. 三文件自洽与重渲染幂等

离线模式（`--snapshot` + `--snapshot-at`，不联网）重渲染 `delivery-tracking.md`，与提交内容逐字节一致，工作区无残留 diff：

```console
$ cd /private/tmp/sn766-gate          # detach 于 cd1fdac6
$ SNAPSHOT_AT="$(python3 -c 'import json; print(json.load(open("docs/plans/github-issues-snapshot.json", encoding="utf-8"))["fetched_at"])')"
$ echo "$SNAPSHOT_AT"
2026-11-14T17:17:14+08:00
$ python3 scripts/dev-env/refresh-implementation-tracker.py \
    --snapshot docs/plans/github-issues-snapshot.json \
    --snapshot-at "$SNAPSHOT_AT" \
    --output docs/plans/delivery-tracking.md
Refreshed 65 requirements: docs/plans/delivery-tracking.md
$ git diff --exit-code -- docs/plans/delivery-tracking.md; echo "GATE_EXIT=$?"
GATE_EXIT=0
```

快照自身结构也自洽：manifest 的 72 个受跟踪号（`#336`、十个 Epic、61 个实施 issue）全部在快照内，Epic 的原生父与 `type:epic` 标签、每个实施 issue 的原生父 Epic 均由 `validate_manifest`/`render` 在渲染时强校验通过；快照记录数 78 = 20 条 open + 58 条已关闭的受跟踪号。

### 2. manifest issue 引用与实时 GitHub 状态对账

实时抓取（2026-11-14 17:2x，`gh issue list --state all` / `gh pr list --state all`）与快照逐号比对：**受跟踪号的 state 差异 0 条**；快照 open 集合与实时 open 集合完全相等（20 条）；快照「未登记 open」6 条与实时未受跟踪 open 项完全相等。矩阵把每条实施 issue 的状态直接渲染自快照状态，故 61 条引用全部一致，无「已关闭仍写未还」或「open 写成已交付」。

抽查/全查混合结果（矩阵原文见 `git show cd1fdac6:docs/plans/delivery-tracking.md`）：

| issue / PR | 实时状态 | 矩阵里的说法 | 一致 |
| --- | --- | --- | --- |
| `#569` | ISSUE closed / completed | closed / completed；验收见需求证据 | 是 |
| PR `#573` | PR merged（2026-11-08） | 正文「随 #569 的 PR #573 落地」 | 是 |
| `#555` | ISSUE closed / completed | closed / completed | 是 |
| `#529` | ISSUE **open** | open；状态标签待核实 | 是 |
| `#540` | ISSUE **open** | open；状态标签待核实 | 是 |
| `#572` | ISSUE **open** | open；状态标签待核实 | 是 |
| `#406` | ISSUE closed / completed | closed / completed | 是 |
| `#704` / `#705` | ISSUE closed / completed | 正文「#704/#705，PR #737」 | 是 |
| `#738` | ISSUE closed / completed | 正文「#738，PR #739」 | 是 |
| `#745` | ISSUE closed / completed | 正文「#745，PR #746」 | 是 |
| `#747` | ISSUE closed / completed | 正文「#747，PR #749」 | 是 |
| `#753` | ISSUE closed / completed | 正文「#753 的主干红，PR #756 恢复」 | 是 |
| `#750` | ISSUE **open** | 未登记 open 区一行 + `W-BOOK-UNIT` 缺口「#750，PR #752 在审」 | 是 |
| `#754` | ISSUE **open** | 未登记 open 区一行 + `W-BOOK-UNIT` 缺口 | 是 |
| `#755` | ISSUE **open** | 未登记 open 区一行 + `W-BOOK-UNIT` 缺口 | 是 |
| `#757` | ISSUE **open** | 未登记 open 区一行 + `U-FILE`/`O-BUILD` 缺口「PR #758 在开」 | 是 |
| `#696` / `#740` | ISSUE **open** | 未登记 open 区各一行 | 是 |
| `#723` / `#702` / `#703` | ISSUE closed / completed | 正文按「已关闭」叙述（PR #724 / #732 / #725 均 merged） | 是 |

关键旁证：三个 open PR 恰为 `#765`（本卡）、`#752`（`#750`）、`#758`（`#757`），与矩阵「PR #752 在审」「PR #758 在开」一致；无第四个在开 PR 被遗漏。

### 3. 新 open 项已登记

「未登记的 open issue」区由快照的未受跟踪 open 项生成，当前 6 行：`#696`、`#740`、`#750`、`#754`、`#755`、`#757`（标题与实时 GitHub 标题逐字一致）。对应残留写进需求行缺口：

- `W-BOOK-UNIT` 缺口：`#750`（PR #752 在审）、`#754`（429 报成结果未知）、`#755`（409 族与 401/403/415 折叠）。
- `U-FILE` 缺口：`#740` 仍 open、`#757`（纯 `web/` 改动不跑 Web 集成分片）。
- `O-BUILD` 缺口 + 集成：`#757`（`mode=web` 不进 `integration_shards`）。

### 4. `F-JOB` 那处事实修正是否正确（本卡核心）

改动前后（`git show cd1fdac6^:docs/plans/delivery-tracking.md` 对比 head）`F-JOB` 行第三列有三处变化：① 本卡核心的语义替换（下详）；② 新增一句公开创建路径的分码事实「413 `job_request_too_large`（#703，PR #725）/ 422 `workspace_not_configured` 与 503 `job_unavailable` 分离（#702，PR #732）」；③ 新增一句 2026-11-14 增量核验（两个契约用例钉住上述两码，并声明未在目标部署实测接收上限）。②③ 已在第 5 节核实，① 删掉的是：

> 残留：该码仍非「未外发」的充分条件（引擎 4xx 与 stage 期结果存储不可用同码却进 `unknown_quarantined`），见 #569。

换成：

> 两桶不变量随 #569 的 PR #573 落地（`execution_cleanup_pending` 承接已外发桶，`FileStageUnavailableError` 归本地拒绝并释放容量）；残留：`invalid_execution_request` 仍非「未外发」的充分条件——泳道在引擎请求之后抛基类 `InvalidExecutionRequestError` 时按保守隔离处理（`file_execution_worker.py` 的 `_run` 只把 `FileCommandInvalidError`/`FileStageUnavailableError` 走 `_reject_without_quarantine`），且该码仍聚合「阶段未配置」这类部署事实；legacy 行读路径不改写，判别需按 state/cleanup/dispatched_at 组合。

**判定：正确，且是必要的假事实删除。** 逐项复核（都在 `origin/main` 的 `fe165d8a` 树上读，非作者陈述）：

1）两桶不变量确实已落地，且是 `domain` 层的机械约束，不只是文档口径：

```console
$ git show origin/main:execution-gateway/src/speechnexus_gateway/domain/submission.py | sed -n '185,205p'
UNSENT_EXECUTION_ERROR_CODES: Final = frozenset(
    {
        ExecutionErrorCode.GATEWAY_BARRIER_FENCED,
        ExecutionErrorCode.INVALID_EXECUTION_REQUEST,
        ExecutionErrorCode.LEASE_NOT_ACTIVE,
        ExecutionErrorCode.POLICY_OR_RESOURCE_DISABLED,
    }
)
#: Codes that may only describe an execution whose engine request may exist.
HELD_EXECUTION_ERROR_CODES: Final = frozenset(
    {
        ExecutionErrorCode.ENGINE_TRANSPORT_UNKNOWN,
        ExecutionErrorCode.CANCELLATION_UNCONFIRMED,
        ExecutionErrorCode.EXECUTION_CLEANUP_PENDING,
    }
)
```

即 `execution_cleanup_pending` 在 held（已外发）桶、`invalid_execution_request` 在 unsent 桶，与改后句子的两个断言同向。对照 `execution-gateway/src/speechnexus_gateway/domain/submission.py:206` 的 `execution_terminal_shape()` 与 `_UNSENT_TERMINAL`/`_HELD_TERMINAL`，`failed` 终态 `holds_capacity` 为假。

2）引擎 4xx 不再借用未外发码，改记 `execution_cleanup_pending`：

```console
$ git show origin/main:execution-gateway/src/speechnexus_gateway/infrastructure/file_execution_worker.py | sed -n '336,352p'
        except EngineRequestRejectedError:
            # A positive client-error refusal is not a timeout and not a retry
            # signal. The request did leave this process, so it stays quarantined
            # and keeps capacity until a coordinated release. The recorded reason
            # is the held clean-up shape; the wire enum has no dedicated
            # engine-refusal code and must not borrow the unsent request code.
            try:
                await self._database(
                    lambda: self._quarantine(
                        key,
                        cancelled=False,
                        reason_code=ExecutionErrorCode.EXECUTION_CLEANUP_PENDING,
                    )
                )
```

3）`FileStageUnavailableError` 确实归本地拒绝、释放容量，且覆盖「阶段结果存储不可用」这条被删掉的旧债务：

```console
$ git show origin/main:execution-gateway/src/speechnexus_gateway/infrastructure/file_execution_worker.py | sed -n '314,333p'
            try:
                outcome = await self._execution.stage_result(...)
            except FileCommandInvalidError, FileStageUnavailableError:
                # These two types prove, before any engine call, that the frozen
                # request cannot be served: a structural command/content refusal or
                # a stage the deployment does not configure. Nothing left this
                # process, so capacity is released without stop evidence. A base
                # ``InvalidExecutionRequestError`` from a lane is deliberately not
                # caught here: it may be raised after an engine request exists, and
                # then the execution must stay held until coordination.
                await self._reject_without_quarantine(key)
                return
```

```console
$ git show origin/main:execution-gateway/src/speechnexus_gateway/file_speech_runtime.py | grep -n "raise FileStageUnavailableError"
98:        raise FileStageUnavailableError("the configured gateway does not serve this file stage")
130:        raise FileStageUnavailableError("the configured file result store is unavailable")
```

第 130 行正是被删句子里「stage 期结果存储不可用」那条，现在走本地拒绝；被删掉的「同码却进 `unknown_quarantined`」在新代码里已不成立。

4）`#569` / PR #573 真实状态：

```console
$ gh api repos/hrygo/SpeechNexus/issues/569 --jq '{state, state_reason, title}'
{"state":"closed","state_reason":"completed","title":"invalid_execution_request 同时表示未外发与已外发未知"}
$ gh api repos/hrygo/SpeechNexus/pulls/573 --jq '{state, merged, merged_at, title}'
{"merged":true,"merged_at":"2026-11-08T02:34:07Z","state":"closed","title":"fix: 终码不再混用「确定性未外发」与「已外发未知」(#569)"}
$ git log --oneline -1 99d07c35
99d07c35 fix: 终码不再混用「确定性未外发」与「已外发未知」(#569) (#573)
$ git branch -r --contains 99d07c35 | head -1
  origin/main
```

squash `99d07c35` 在 `origin/main` 上，且提交正文写明「引擎 4xx 改记 execution_cleanup_pending，不再借用未外发码」「阶段结果存储不可用各自给出确定码」「legacy 行按原样读取」，与改后句子一一对应。

5）改后句子与被审 PR 未改动的 `X-GATEWAY` 行不再互相矛盾。`X-GATEWAY` 在改动前就已写「终态两桶不变量已落地（#569 / PR #573）…」，`F-JOB` 却仍说「同码却进 `unknown_quarantined`」——这是同一份矩阵内部的事实打架，本轮删除的是其中错的那一侧。

6）保留的三条残留也是真的：

- 基类 `InvalidExecutionRequestError` 在引擎请求之后可能由泳道抛出，`_run` 故意不接（上面的注释即证据），落最外层 `except Exception, asyncio.CancelledError` → `_quarantine`，属保守隔离、占容量。
- `invalid_execution_request` 仍聚合部署事实：`execution-gateway/src/speechnexus_gateway/file_speech_runtime.py` 的「该阶段未配置」分支（`raise FileStageUnavailableError`）在 `validate_request` 期抛出，被 `dispatch()` 的 `except InvalidExecutionRequestError` 归为 `DispatchResult.REJECTED` 并记 `INVALID_EXECUTION_REQUEST`；同一处现场注释亦写明「an unconfigured stage is a deployment or routing mismatch…it stays here until a dedicated execution error code exists」。
- legacy 行不改写、需按 state/cleanup/dispatched_at 组合判别：`docs/components/execution-gateway.md:402` 明确「该不变式是**写入时**约束…读路径不改写历史行」，`:404`–`:416` 给出旧组合判别顺序与 `unknown_quarantined/quarantined + invalid_execution_request` 的已知旧组合。这也正是改后句「该码仍非未外发的充分条件」的准确依据。

### 5. 是否引入新「假事实」

未发现。改后句子引用的符号与行为逐个存在且描述准确：

| 引用 | 存在性/行为 | 结论 |
| --- | --- | --- |
| `file_execution_worker.py` 的 `_run` | 存在（`async def _run(self, ticket, intent)`） | 准确 |
| `_reject_without_quarantine` | 存在；实现为 `self._reject(key)` | 准确 |
| `FileCommandInvalidError` / `FileStageUnavailableError` 两个例外类 | 均存在于 `domain/file_execution.py`，且都是 `InvalidExecutionRequestError` 子类 | 准确 |
| 「只把这两个类走 `_reject_without_quarantine`」 | `_run` 内走该调用的类型集合确实只有这两类（内层 `except` 元组 + 外层 `except FileCommandInvalidError`） | 准确（措辞可再精确，见建议 2） |
| `execution_cleanup_pending` 承接已外发桶 | `HELD_EXECUTION_ERROR_CODES` 成员；`EngineRequestRejectedError` 与成功但缺停止证据两处都记它 | 准确 |
| `_quarantine(reason_code=EXECUTION_CLEANUP_PENDING)` 释放语义 | `_quarantine` 走 `mark_execution_unknown`，落 `unknown_quarantined`/`quarantined`，占容量 | 准确 |
| 413 `job_request_too_large`（#703，PR #725） | `job_errors.py` 声明、`jobs.py` 返回；PR #725 merged、#703 closed | 准确 |
| 422 `workspace_not_configured`，与 503 `job_unavailable` 分离（#702，PR #732） | 两个码分别在 `job_errors.py` 声明（422 / 503）；PR #732 merged、#702 closed | 准确 |
| `tests/contract/test_public_job_body_bound.py`、`test_public_job_workspace_default.py` 钉住声明与分码 | 两文件均存在，且各自命中对应码 | 准确 |

同一轮新增的其它文字也一并核过（不是本卡核心，但属同一 PR 的新事实）：`MAX_SPEECH_TEXT_CHARS = 4096`（`speech_synthesis.py`）、`RenderUnitText.maxLength: 200000` 与 `render_units` CHECK `length(text) <= 200000`、同步 400 `invalid_request` + `param: input` / 异步 422 `unsupported_parameter`、422 `render_submission_rejected` 投影、`render-unit-bounds.ts`/`render-unit-bounds.test.ts`/`RenderUnitPanel.test.tsx`、`tests/web/file-task.behavior.spec.mjs` 与根目录 `playwright.behavior.config.mjs`、CI 在既有 Web 作业内顺序追加 `pnpm test:behavior` 且两门禁共用 4173/4174 + `--strictPort`、`scripts/dev-env/check-review-pr-boundary.py` 的 `REVIEW_ROOT`/`TITLE_PREFIXES`/merge-base 端点、`tests/ci/test_review_pr_boundary.py`、运行 `43570172238`/`43570366503` 内「Reject review pull requests that carry non-review paths」步骤均 success，以及「主干 `26fd8b39` 的 push CI 全绿（含 Integration 4/5 与 Web 作业）」（push 运行 `43575396153` 全部 success）。以上均成立。

### 6. 证据链接

矩阵证据区在本 PR 中未新增/改动链接（`40 insertions` 全是文本与时间戳；证据列仅在整行重写时随行出现，内容未变）。全量核验（不是抽样）：

```console
$ # 从矩阵抽 https://github.com/hrygo/SpeechNexus/blob/<sha>/<path>，逐条 git cat-file -e
blob link instances: 189 unique: 123
resolvable: 123 broken: 0
all shas exist: True
```

manifest 的 223 条证据条目（`blob` 189、`pull` 28、`issues` 5、`actions` 1，去重 145 个 URL）中，全部 `blob` 目标在各自引用的 sha 下存在。抽样 10 条（均为跨较多 commit 的旧证据，最易失效）：

| 引用 | 结果 |
| --- | --- |
| `blob/3ac2bfc/backend/src/speechnexus_backend/speech/interfaces/http/speech.py` | 存在 |
| `blob/3ac2bfc5da2442305ba05be53c8a8cde17668a6f/web/workbench/src/App.tsx` | 存在 |
| `blob/0b8e85a5/execution-gateway/src/speechnexus_gateway/infrastructure/submit_sql.py` | 存在 |
| `blob/cef394336929e4f520d11ebe962034ffc7dd02e2/deploy/compose.yaml` | 存在 |
| `blob/f12baad66db66f05af3f1de0130164168e6271ad/tests/integration/test_public_job_list_pg.py` | 存在 |
| `blob/7182911f81811c25e0dab546f54fdc80303d9c34/contracts/speech-openapi.yaml` | 存在 |
| `blob/eac21eb563efa65014b07db49ef2f638ec8083e6/backend/src/speechnexus_backend/workbench/interfaces/http_render_units.py` | 存在 |
| `blob/672381611e48fe0c57200b861d48660c9dcb58d3/tests/integration/test_pg_dsn_skip_guard.py` | 存在 |
| `blob/6544a934/scripts/dev-env/minio-test-service.sh#L43` | 存在（文件存在；`#L43` 锚点为行内链接） |
| `blob/3dd82415ba728ea5a676b646e4108b9f5d5b29b4/contracts/README.md` | 存在 |

### 7. 行号纪律

本轮新增的 40 行里**没有新增裸行号**：

```console
$ git show cd1fdac6 -U0 -- docs/plans/ | grep '^+' | grep -E '(:[0-9]+\b|#[Ll][0-9]+|第[0-9]+行|line [0-9]+)'
（仅命中两处时间戳 17:17:14+08:00 / 2026-11-14T17:17:14+08:00 与 O-BUILD 整行重写中原样保留的 #L43）
```

新增文字一律用符号名/文件名定位：`file_execution_worker.py` 的 `_run`、`_reject_without_quarantine`、`render-unit-bounds.ts` 的 `renderUnitLength`、`MAX_SPEECH_TEXT_CHARS`、`check-review-pr-boundary.py`、`playwright.behavior.config.mjs`、`tests/...` 路径。唯一带行锚点的两处（`minio-test-service.sh#L43`、`asset_runtime.py#L115`）在改动前就存在且本 PR 未修改该文本。

### 8. `check-doc-refs.py` 前后对比

在 `26fd8b39`（改前）与 `cd1fdac6`（改后）两个干净检出上分别全量跑：

```console
$ cd /private/tmp/sn766-base && python3 scripts/dev-env/check-doc-refs.py | tail -2
SUMMARY refs=824 AMBIGUOUS=53 EXTERNAL=1 MISSING=3 OK=767
RESULT FAIL

$ cd /private/tmp/sn766-gate && python3 scripts/dev-env/check-doc-refs.py | tail -2
SUMMARY refs=824 AMBIGUOUS=53 EXTERNAL=1 MISSING=3 OK=767
RESULT FAIL
```

计数逐字相同；`--problems-only` 的 `[MISSING]` 行 `diff` 无输出，三条完全一致，全部落在 `docs/reviews/`（本 PR 未碰的目录）。为避免本文档自己再造行号引用，下面用「第 N 行」转述：

| 引用的文档 | 被引用的写法 | 工具判定 |
| --- | --- | --- |
| `docs/reviews/2026-11-13-workbench-tts-probes.md` 第 48 行 | `orphan_probes.py` 第 415 行 | MISSING（仓库内不存在该文件） |
| `docs/reviews/2026-11-13-workbench-tts-probes.md` 第 110 行 | `orphan_probes.py` 第 419 行 | MISSING（仓库内不存在该文件） |
| `docs/reviews/2026-11-14-text-limit-alignment.md` 第 68 行 | `backend/src/.../workbench/infrastructure/public_speech_render_jobs.py` 第 45 行 | MISSING（`...` 省略式路径不可解析） |

第 3 条的判定（作者表述为「裸文件名歧义判 MISSING」）**结论对、归因不准**：

- 该引用不在正文，而在 `docs/reviews/2026-11-14-text-limit-alignment.md` 的 ```console``` 代码块里，是把 `rg` 输出粘贴时用 `backend/src/.../workbench/...` 省略了中间路径段。脚本按字面路径找不到 → MISSING，机械上没错（`REF` 正则把 `...` 前的段当路径）；报的却是「裸文件名，唯一匹配 …」，措辞本身就是脚本侧的不精确。
- 语义上这条引用**不坏**：`backend/src/speechnexus_backend/workbench/infrastructure/public_speech_render_jobs.py` 存在，其第 45 行正是 `async def submit(self, *, unit: RenderUnit, key: str | None, reference: str) -> str:`，与被引内容一致。
- 所以它既非本 PR 引入，也不是「行号错」，而是「省略式路径不可点击」；修它属于那份评审文档的自洽性，超出本 PR「只改三文件」的范围。

### 9. 离线渲染门禁幂等

同一命令连续两次重渲染，MD5 一致，且等于提交内容（无「编辑中间态」残留）：

```console
$ md5 -q delivery-tracking.md            # 提交内容
6a4292362f7539ef6eb2b4573e36d0e3
$ # 第一次重渲染后
6a4292362f7539ef6eb2b4573e36d0e3
$ # 第二次重渲染后
6a4292362f7539ef6eb2b4573e36d0e3
$ cmp <(git show cd1fdac6:docs/plans/delivery-tracking.md) docs/plans/delivery-tracking.md && echo IDENTICAL
IDENTICAL
```

`ruff format --check`：

```console
$ uv run --no-project ruff format --check docs/plans/delivery-tracking.md
1 file already formatted
```

PR #765 自身的 CI 也印证：`Docs: delivery matrix freshness` pass（运行 `43580030787`）。

### 10. 范围

```console
$ git show cd1fdac6 --name-only --format=""
docs/plans/delivery-issues.json
docs/plans/delivery-tracking.md
docs/plans/github-issues-snapshot.json
$ git show cd1fdac6 --name-only | grep -c uv.lock
0
```

只改三文件；未碰代码、契约、CI、脚本；`uv.lock` 不在 PR 内（主工作区里 `uv.lock` 的改动是本地脏状态，与本 PR 无关，作者也未纳入提交）。

### 11. 作者对三处输入事实的纠正

逐条 `gh` 核实，**三条都对**：

| 作者纠正 | 实测 | 结论 |
| --- | --- | --- |
| `#740` 是仍 open 的 issue（PR #741 已合并但未关闭它） | `gh api .../issues/740` → `{"state":"open",...}`；`gh api .../pulls/741` → `merged: true`（2026-11-14T06:29:35Z） | 正确 |
| `#429` 是已关闭、未合并的 PR（`pull_request != null`），不是 issue | `gh api .../issues/429` → `{"has_pull_request":true,"state":"closed"}`；`gh api .../pulls/429` → `merged: false`、标题「ci: add opt-in OpenCode PR reviews」 | 正确 |
| `#724` 是已合并的 PR（修复 `#723`），不是被关闭的 issue | `gh api .../pulls/724` → `merged: true`（2026-11-14T02:56:39Z），标题「fix: 取消路径的终态拒绝也持久结算，重载不再复活成未知 (#723)」；`#723` 是 closed issue | 正确 |

旁证：`scripts/dev-env/check-review-pr-boundary.py` 的模块注释本身就把 PR #429 列为「`review:` 近似命中但是实现 PR」的已知例子，与作者的纠正同向。

## 必须修清单

无阻断项。

建议（不阻断合并，均属措辞精确性；改不改都不影响矩阵事实成立）：

1. **PR 描述**：第 3 条 MISSING 的归因由「裸文件名歧义判 MISSING」改为「console 片段里用 `...` 省略了中间路径，脚本按字面解析判 MISSING；目标文件与第 45 行实际正确」。这是本卡唯一一处**表述与事实不符**，但落在 PR 描述、不在三文件里。
2. **`F-JOB` 句**：「`_run` 只把 `FileCommandInvalidError`/`FileStageUnavailableError` 走 `_reject_without_quarantine`」可改为「`_run` 的两个拒绝分支只承接 `FileCommandInvalidError`/`FileStageUnavailableError`」——`_run` 里其实有两个走该调用的分支（内层 `except` 元组与外层 `except FileCommandInvalidError`），类型集合的断言成立，但「只把」易被读成「只有一个入口」。
3. **`F-JOB` 句**（可选）：「非未外发充分条件」的因果链可拆开写成两句——(a) 该码仍聚合「阶段未配置」这类部署事实；(b) 泳道在引擎请求之后抛基类时只能保守隔离，不能据该码断言未外发；(c) legacy 行需按 state/cleanup/dispatched_at 判别。现句把 (b) 放在最前，读者容易误推「新写入也可能把已外发的执行记成 `invalid_execution_request`」——代码里新写入被 `UNSENT_EXECUTION_ERROR_CODES`/`HELD_EXECUTION_ERROR_CODES` 与写入期跨桶拒绝挡住，不会。

## 只能依赖作者陈述、本次无法独立复核的点

1. **快照抓取时刻的瞬时内容**：我能证明的是「当前实时状态与快照 0 差异」「重渲染逐字节一致」「结构性校验通过」，不能证明 `2026-11-14T17:17:14+08:00` 那一刻抓到的原始响应未被人工改动。若需闭环，只能重跑 `--refresh-snapshot` 联网抓取（会写回文件，按本卡约束不做）。
2. **作者的「改前」基线**：我按 `cd1fdac6^`（`26fd8b39`）复现，与作者声明的改前基线一致；更早历史未复核（无必要）。
3. **CI 侧的 freshness 作业跨环境一致性**：我在 macOS 本地 `python3` 跑同一命令通过；PR #765 的 CI 作业结论为 pass（我只读 check-run 结果，未重放 ubuntu-24.04 环境）。
4. **`26fd8b39` push CI 全绿**这一句：我核到 push 运行 `43575396153` 的全部作业 success（含 Integration 0–5 与 Web），但未逐条读作业日志。

## 过程记录

- 17:22 建独立 worktree `/private/tmp/sn766`（`review/766-matrix-refresh` @ `origin/main` `fe165d8a`），确认 `git diff origin/main...HEAD` 为空。
- 17:24 取 `cd1fdac6` 三文件旧/新版本到 `/tmp/rev766`，按 `git show` 读取，逐单元格对比；定位 `F-JOB` 行第三列唯一语义变更。
- 17:26 读 `origin/main` 的 `file_execution_worker.py`、`submission.py`、`file_execution.py`、`file_speech_runtime.py`、`recovery_sql.py`，核 `_run` 的四个分支、两个桶常量与 `reject_execution_before_engine` 的写入形状；核 `#569`/PR #573/`99d07c35`。
- 17:28 建 `/private/tmp/sn766-gate`（detach `cd1fdac6`）跑 CI 同命令的离线渲染门禁，`git diff --exit-code` = 0；连续两次渲染 MD5 一致。
- 17:29 建 `/private/tmp/sn766-base`（detach `26fd8b39`），两树分别跑 `check-doc-refs.py`，`diff` MISSING 列表一致；`ruff format --check` 通过。
- 17:30 `gh` 全量抓 issues/PRs，把 61 条 manifest 引用逐号对账（0 反例），核 6 个新 open 项的标题与残留位置，核三处输入事实纠正。
- 17:31 全量核 123 个唯一 `blob` 证据目标（`git cat-file -e`），0 失效；核新增文字无新行号。
- 17:32 读 `docs/components/execution-gateway.md` 的终码形状表与 legacy 判别顺序，确认 `F-JOB` 保留的三条残留都有权威依据。
- 17:33 核对 PR #765 check-run（freshness pass）与 `26fd8b39` push CI。
- 结论：通过，无阻断项。
