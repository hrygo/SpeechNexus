# PR #804 非作者审查：三个面板对已结算拒绝的重放口径收口

| 项 | 值 |
| --- | --- |
| 审查对象 | [PR #804](https://github.com/hrygo/SpeechNexus/pull/804) `fix/803-panel-replay-parity`（修 issue #803） |
| 被审 head | `2eaed85075d2dd3f6d277f05e117b6f5bacbe4c4`（含 `10dd9b0e` 主改动 + `2eaed850` 覆盖表重派生） |
| 基线 | `origin/main` = `3b7abd51cc8aee5b0af5aff83667bb6656a9e05f` |
| 审查分支 | `review/805-panel-parity`（独立 worktree `/private/tmp/sn805`，`git worktree add -b review/805-panel-parity origin/main`） |
| 运行环境 | 独立 detached worktree `/private/tmp/sn805-pr`（`2eaed850`，自建 `.venv` + `pnpm install --frozen-lockfile`），不借用作者 worktree |
| 审查时间 | 2026-11-15 05:26–05:50（Asia/Shanghai） |
| 结论 | **通过（Approve），无必须修项** |

审查方式：被审内容一律用 `git show 2eaed850:<path>` 读取；需要运行的分支在 `/private/tmp/sn805-pr` 上执行；
所有变异只在临时副本上做并已还原（`git status` 干净），未 push 任何变异。
本报告文件本身是审查分支相对 `origin/main` 的唯一改动。

---

## 结论

三处「保留原始 Idempotency-Key 的 create」在**读取侧**确实收敛到一条规则（`refusedReplayable`），
写入侧保留渲染面板的逐码表（因为它还要为每个码渲染处置步骤），两者的等价由
`tests/contract/test_render_refusal_vocabulary.py` 三方钉住——我独立造了面板侧与后端侧两个方向的变异，都如期变红。
穷尽性断言、5xx/408/429 分类、三条作者自报反例、例数与 CI 全部实测复核一致；改动范围干净。
不存在需要返工的缺陷。

同时记录三处**非阻断**观察（合同字段定义域、导出/助手侧 `retryable` 恒 false 未被测试钉住、parity 表只覆盖读取侧），
见「建议」。

---

## 核心三问

### 1. `error.retryable` 是否真等价于「可按原键重放」

**答：在它被读取的定义域（已结算 4xx、且非 408/429）内等价；作为该字段的全局语义不成立——代码用 `settled` 守卫生效，所以不是缺陷。**

证据分三层：

1. **字段在这门 API 里的定义原文**（`contracts/workbench-openapi.yaml:2262-2267`，`generateRenderUnitV2` 描述）：

   > `error.retryable` states whether **the same dispatch** may be retried after the refusal, and it belongs to the disposition rather than to the status: a cleanup wait is retryable and a spent key is not, though both answer 409. A caller must read it instead of inferring retryability from the status.

   这正是「按原键重放」的口径（same dispatch = 同 path/body/key），不是通用 HTTP 的「重试这个请求」。
   `contracts/shared/errors.schema.json` 只声明 `retryable: {type: boolean}`（`required`，无 description）——
   **定义的落点在操作描述，不在共享 schema**。这是下面「建议 S1」的由来。

2. **反例排查（两个方向都查了）**：

   - *`retryable: true` 但不该按原键重放？* 工作台全 API 只有两个可重试处置，且都由渲染派发独占：
     `render_submission_rate_limited`（429）与 `render_submission_cleanup_pending`（409）。
     前者被 `settled` 排除（429），后者语义就是「等清理完再发同一次派发」，同键重放正确。
     **未发现反例。**
   - *`retryable: false` 但其实该按原键重放？* **有一处，且是真实语义分歧**：
     - 工作台 `503 outcome_unknown` 是 `retryable: false`，但契约同时要求「只有保留原键的调用方可以用同一次 POST 重放来取回原始已接受结果」
       （`contracts/workbench-openapi.yaml`：「Only a caller retaining the original Idempotency-Key may replay this same POST … to recover the original accepted result」）。
     - 上游 speech Job 的同一字段用的是**另一套语义**：`backend/src/speechnexus_backend/speech/interfaces/http/job_errors.py:143`
       `retryable = code in ("job_outcome_unknown", "job_capacity_exhausted", "job_admission_contended")`，
       所以 `execution_cleanup_pending`（409）在上游是 `retryable: false`，而正确动作恰是「等清理完成、同键重发」。
       工作台刻意与上游分歧并在 `http_render_units.py:126-145` 写明了理由；渲染面板因此**不读该字段**，读自己的逐码表。
     - 这两条都落在 `settled` 之外（`503`）或本卡不读字段的路径（渲染）上，所以没有变成 bug：
       实测 5xx/408/429 一律 `refused === null`（未知）且重放可用（见下文 C7 探针 7/7）。

3. **导出/助手侧字段实际恒为 false**：`_error()` 的 `retryable` 默认 `False`
   （`backend/src/speechnexus_backend/workbench/interfaces/http_versions.py:281`），全仓库唯一显式传 true 的调用点是
   `http_render_units.py:385` `retryable=refusal_retryable(code)`。导出/助手的 endpoint（`http_exports.py:96-117`、
   `http_assistant.py:104-127`）全部走默认值。所以读取该字段在可达面上等价于「终止」，且**不是第二份码表**——这一点作者陈述成立。

   但这也意味着：**「导出/助手 4xx 的 retryable 恒 false」目前没有任何测试钉住**（见「建议 S2」）。

### 2. 穷尽性断言是否真的失败

**答：是，实测红。**

我把 `RecordedRefusal` 临时加一个成员（`| "expired"`，只在临时 worktree，已还原），
`cd web/workbench && pnpm typecheck` 立即失败，红点正是断言位置：

```
$ pnpm typecheck
src/assistant/AssistantHost.tsx(16,57): error TS2366: Function lacks ending return statement …
src/books/BookExportPanel.tsx(33,57): error TS2366: Function lacks ending return statement …
src/books/recovered-intent.ts(91,5): error TS2366: Function lacks ending return statement …
src/recovery/refusal-parity.test.tsx(191,7): error TS2741: Property 'expired' is missing in type
  '{ none: …; retryable: …; terminal: …; unclassified: …; }'
  but required in type 'Record<ScenarioKey, { readonly refused: RecordedRefusal | null; … }>'.
[ELIFECYCLE] Command failed with exit code 2.
```

`refusal-parity.test.tsx:190-191` 的 `type ScenarioKey = RecordedRefusal | "none"; const SCENARIOS: Record<ScenarioKey, …>`
是**真**的编译期穷尽门；三个面板的 `switch` 也同时报 TS2366。

### 3. 5xx 会不会被误判成终态拒绝

**答：不会，实测。**

`settled` 判定要求 `400 <= status < 500` 且状态不在 `{408, 429}`（`book-export.ts:193`、`assistant-session.ts:253`），
5xx 一律 `settled = false` → `refusedCategory` 返回 `null` → `journal.failure(null)` → `uncertain = true`、`refused = null`
→ `refusedReplayable(null) = true`。我在临时 worktree 写了一个一次性探针（用完即删，未提交），实跑 **7 tests passed**：

| 场景 | `refused` | `problem` | 重放是否真的重发 |
| --- | --- | --- | --- |
| 导出 500 `internal_error` | `null` | `unknown` | 是（create 调用 2 次） |
| 导出 503 `outcome_unknown` | `null` | `unknown` | 是 |
| 导出 408 `request_timeout` | `null` | `unknown` | 是 |
| 导出 429 `render_submission_rate_limited`（retryable=true） | `null` | `unknown` | 是 |
| 助手 500 `internal_error` | `null` | `unknown` | 是 |
| 助手 503 `outcome_unknown` | `null` | `unknown` | 是 |
| 对照：导出 422 `invalid_change`（settled 4xx） | `terminal` | — | 否（create 只 1 次） |

即 5xx/408/429 全部落 `unknown` 且保留原键，**没有任何 5xx 变成终态拒绝**；`429` 也没被误记成「已结算拒绝」。

---

## 逐条判定

### A. `error.retryable` 与「按原键重放」的等价性（最重要）

- **A1 契约定义**：✔ 见核心三问 1。`generateRenderUnitV2` 描述原文是 "whether the same dispatch may be retried after the refusal"；
  共享 schema 中 `retryable` 是 `required` 的 boolean、**无 description**，定义由操作承载。
- **A2 反例排查**：✔ 未发现必须在 `settled` 4xx 域内返工的反例；发现并核实了一处**跨服务**语义分歧（speech Job 的 `execution_cleanup_pending`
  `retryable:false` vs 工作台 `retryable:true`）与一处**同 API 内**分歧（`503 outcome_unknown` `retryable:false` 却须按原键重放）。
  两者都被 `settled` 守卫或「渲染读逐码表」的既有设计挡住，且 `http_render_units.py:126-145`、`workbench-client.ts:8-41`
  已写明「它回答『同一请求能不能再发』，不回答『未知结果能不能重放』」。**判定：不需必须修**；建议 S1 把「定义域只有渲染操作」写成一句话。
- **A3 三方一致性是否真被钉住**：✔ 独立做了两个方向、三个变异，全部如期红（见「实测输出」）。
  钉点是 `tests/contract/test_render_refusal_vocabulary.py:401`（面板 `RENDER_REFUSAL_RETRYABLE` == 后端 `_RETRYABLE_REFUSAL_CODES`）
  与 `:426`（按 status×upstream 全网格反推 `refusal_retryable`），且 `_RETRYABLE_REFUSAL_CODES` 就是
  `refusal_retryable()` 与 route 传参用的同一个对象（`http_render_units.py:207,385`），不是装饰性常量。

### B. 穷尽性断言与真挂载

- **B4**：✔ 见核心三问 2（TS2741 实测）。
- **B5 是否真挂载三个面板**：✔ `refusal-parity.test.tsx` 直接 `import { AssistantHost } / { BookExportPanel } / { RenderUnitPanel }` 并
  `render(...)` 真实组件；只 mock 了 `WorkbenchClient` 的取数方法，**journal/sessionStorage/组件状态全是真路径**（`retained()` 直接写
  `sessionStorage` 的三个真实 journal key）。反向证据：我把真实 `BookExportPanel.tsx` 的 `disabled` 改回 `state.busy`，
  parity 的 `terminal`/`unclassified` 两行立即红——如果挂的是假组件不会红。

### C. 反例与边界

- **C6 三条反例独立复现**：✔ 计数与红点全部与作者一致（4 / 8 / 5，见实测输出）。三条都在已提交树上做、已还原、未 push。
- **C7 `settled = 4xx 且非 408/429`**：✔ 408/429 → `unknown`（可重放）；其他 5xx → `unknown`（可重放，不是终态）；只有非 408/429 的 4xx 才是
  refusal 并交给 `retryable` 分类。与渲染面板既有口径（`RenderUnitPanel.tsx:158, 293, 300-310`）同形。
- **C8 助手侧 429 不可达**：✔ 工作台错误表里 429 只有 `render_submission_rate_limited` 一个码（`http_versions.ERRORS`），
  唯一产生点是渲染派发；`http_assistant.py` 的异常分支只映射 IngressError / LlmError / 404 / 409 / 422 / 503，无 429。

### D. 范围与例数

- **D9 范围**：✔ 15 个文件 = 覆盖证据表 1 + `web/workbench/src/{recovery,books,assistant,manuscripts}` 里 14 个（含同目录测试）。
  未触碰 `contracts/`、`backend/`、`.github/`、`scripts/`、`tests/`；`pnpm-lock.yaml`、`uv.lock` 均无改动；
  `#755` 分类文件 `books/render-refusals.ts` 未被改；journal 记录形状（`RECORD_KEYS`/`LEGACY_KEYS`/`version:1`/`uncertain`）逐行核对**只有两条纯函数新增**：

  ```text
  +export function refusedCategory(settled: boolean, retryable: boolean): RecordedRefusal | null
  +export function refusedReplayable(refused: RecordedRefusal | null): boolean
  ```

- **D10 例数与 CI**：✔ 全部实测（见下表）；CI 首轮的 `tests_drift` 也独立确认，修法是重派生而不是手改表。

| 检查 | 实测（PR `2eaed850`） | 实测（基线 `3b7abd51`） | 作者陈述 |
| --- | --- | --- | --- |
| `cd web/workbench && pnpm test` | **1063 passed / 65 files** | **1043 passed / 64 files** | 1063 / 65（基线 1043 / 64）✔ |
| `pnpm build && pnpm test:behavior` | **18 passed** | **18 passed** | 18（与基线相同）✔ |
| `pnpm test:a11y` | **52 passed** | 未复跑（见「只能依赖作者陈述的点」） | 52 |
| `pnpm typecheck` | 通过（基线态） | — | 全绿 ✔ |
| `gh pr checks 804` | 18 pass / 2 skipping，0 fail | — | CI 全绿 ✔ |

`tests_drift` 复核：

- 首轮失败的 run 是 `43640344695`（head `10dd9b0e`）。其 `Python: tests, coverage and package build`（job `132586446741`）日志：
  `FAILED tests/unit/test_operation_coverage.py::test_committed_map_is_current`，findings `code: 'tests_drift'`，
  例如 `id: generateRenderUnitV2` 期望测试集合里出现 `web/workbench/src/recovery/refusal-parity.test.tsx`。
- 修法正确性：我在 `2eaed850` 上重跑 `python scripts/dev-env/check-operation-coverage.py --write`，
  与提交版本相比**只差 `audited_head` 一个字段**（该字段按脚本注释记录「派生时的 base」，`differences()` 明确不比较它，`--write` 在提交前运行，
  所以永远指向前一个 commit），其余 52 个操作的 `consumers`/`tests`/`status` 完全一致；
  默认检查（无 `--write`）输出 `{"operations": 52, "bound": 42, "pending": 10, "findings": []}`，`tests/unit/test_operation_coverage.py` 43 passed。
  **结论：是派生结果，不是手改表。**

---

## 必须修清单

**无。**

---

## 建议（非阻断，供后续任务）

- **S1**：`error.retryable` 的「same dispatch」定义只写在 `generateRenderUnitV2` 的操作描述里，`contracts/shared/errors.schema.json` 只有
  `type: boolean`；仓库里同一字段至少存在两种语义（speech Job 的 allow-list 含 `job_outcome_unknown`、不含 `execution_cleanup_pending`）。
  建议在共享 schema 的 `retryable` 上补一句「含义由所属 operation 定义」，或把 `workbench-client.ts` 的注释点明「该定义来自渲染操作描述，
  导出/助手路由实际恒为 false」，避免下一个消费者把它当成全仓统一承诺。
- **S2**：目前没有测试钉住「导出/助手的 4xx `retryable` 恒为 false」（只有渲染词表的三方钉）。今天可达面等价，但如果将来有人给
  `http_exports.py`/`http_assistant.py` 的 `_error(...)` 传 `retryable=True`，面板会直接开原键重放且**没有任何测试会红**。
  建议在 `tests/contract` 增一条断言：这两条路由的 `_error` 调用从不传 `retryable`，或直接对它们的 4xx 网格断言 `retryable is False`。
- **S3**：parity 表只覆盖「恢复记录」的读取侧；写入侧仍是两条路径（渲染用 `keepsReplay` 逐码表，导出/助手用 `refusedCategory(settled, error.retryable)`），
  其等价由 `tests/contract` 钉渲染表 == 后端 set。可在 `refusal-parity.test.tsx` 的文件注释里补一句「本表不覆盖写入路径」，
  避免下一个人误以为整条链路都在这一张表里。

---

## 只能依赖作者陈述的点

1. `pnpm test:a11y` 的**基线** 52：我只在 PR head 上跑了 52 passed，没有在 `origin/main` 上复跑 a11y 基线（与 behavior 不同，a11y 我按「PR 侧达标」验收）。
2. `#755` 的逐码处置文案本身未重新审：该文件（`books/render-refusals.ts`）本卡未改，渲染行为由现有 65 文件/1063 用例与 18 条 behavior 覆盖，
   我未逐码复核其文案与后端步骤的一一对应（只核了可重试集合与词表集合的三方一致）。
3. `render_submission_cleanup_pending`「当前不可达」：我依据契约散文与 `tests/contract/test_render_refusal_vocabulary.py`
   的 AST 反推断言（`test_the_unreachable_mapping_is_declared_and_still_unreachable`），没有构造真实上游调用去证明其不可达。
4. 助手/导出侧 429「不可达」：由代码可达性枚举得出（无 429 产生点），未构造运行期请求验证。
5. 作者自报「未改契约/后端/工作流」：这一点由我独立核对 diff 范围确认，不属陈述依赖。

---

## 实测输出（关键片段）

### A3 三方一致性变异（临时副本，已还原）

面板侧把 `render_submission_conflict` 加进 `RENDER_REFUSAL_RETRYABLE`：

```text
E  AssertionError: the panel's RENDER_REFUSAL_RETRYABLE must list exactly the backend's retryable dispositions
E  Extra items in the right set:
E  'render_submission_conflict'
FAILED tests/contract/test_render_refusal_vocabulary.py::test_the_retryable_dispositions_agree_on_all_three_sides
1 failed, 24 passed
```

面板侧把不存在的码 `render_submission_invented_code` 加进 `RENDER_REFUSAL_CODES`：

```text
E  AssertionError: every render_submission_* code in the backend table must be listed in
   web/workbench/src/books/render-refusals.ts and vice versa …
E  Extra items in the right set:
E  'render_submission_invented_code'
FAILED tests/contract/test_render_refusal_vocabulary.py::test_the_backend_and_the_panel_agree_on_the_vocabulary
1 failed, 24 passed
```

后端侧把 `render_submission_conflict` 加进 `_RETRYABLE_REFUSAL_CODES`：

```text
E  +  where True = refusal_retryable('render_submission_conflict')
FAILED tests/contract/test_render_refusal_vocabulary.py::test_the_retryable_dispositions_agree_on_all_three_sides
FAILED tests/contract/test_render_refusal_vocabulary.py::test_the_retryability_of_every_status_and_upstream_code_is_the_stated_one
FAILED tests/contract/test_render_refusal_vocabulary.py::test_the_named_retry_directions_are_pinned[409-job_retry_not_allowed-render_submission_conflict-False]
3 failed, 22 passed
```

基线：`tests/contract/test_render_refusal_vocabulary.py` **25 passed**（独立 `.venv`，`uv pip install -e packages/contracts -e backend` 后可跑）。

### C6 三条反例（计数与红点）

**反例 1**：`BookExportPanel.tsx` 改回 `disabled={state.busy}`（任意 4xx 都给重放）

```text
Test Files  2 failed | 63 passed (65)
Tests  4 failed | 1059 passed (1063)
FAIL  src/books/BookExportPanel.test.tsx > withholds the same-key replay for a settled refusal and says what it holds
FAIL  src/books/BookExportPanel.test.tsx > reads a record written before #803 as unclassified, without claiming determinism
FAIL  src/recovery/refusal-parity.test.tsx > … > a terminal record reads the same in the render, export and assistant panels
FAIL  src/recovery/refusal-parity.test.tsx > … > a unclassified record reads the same in the render, export and assistant panels
```

**反例 2**：`refusedReplayable` 的 `unclassified` 改向（`return refused !== "terminal"`）

```text
Test Files  7 failed | 58 passed (65)
Tests  8 failed | 1055 passed (1063)
FAIL  src/assistant/AssistantHost.test.tsx > reads a record written before #803 as unclassified, without claiming determinism
FAIL  src/assistant/assistant-session.test.ts > … > restores a record written before #803 as unclassified and withholds the replay
FAIL  src/books/BookExportPanel.test.tsx > reads a record written before #803 as unclassified, without claiming determinism
FAIL  src/books/RenderUnitPanel.test.tsx > … > names a retained-intent control in the note only when its state renders it
FAIL  src/books/RenderUnitPanel.test.tsx > … > keeps a record written before #798 unreplayable, and says only what it holds
FAIL  src/books/book-export.test.ts > … > restores a record written before #803 as unclassified and withholds the replay
FAIL  src/recovery/command-journal.test.ts > … > reads one record one way: only an unspent key or a retryable refusal may be re-sent
FAIL  src/recovery/refusal-parity.test.tsx > … > a unclassified record reads the same in the render, export and assistant panels
```

（渲染面板的两条旧记录用例同时红，说明它读的也是 `refusedReplayable` 这条规则。）

**反例 3**：导出会话改回写 `unclassified`（`this.#journal.failure(settled ? "unclassified" : null)`）

```text
Test Files  2 failed | 63 passed (65)
Tests  5 failed | 1058 passed (1063)
FAIL  src/books/BookExportPanel.test.tsx > withholds the same-key replay for a settled refusal and says what it holds
FAIL  src/books/BookExportPanel.test.tsx > keeps the replay for a refusal the contract calls retryable
FAIL  src/books/book-export.test.ts > … > writes the category the contract states and reads it back after a reload
FAIL  src/books/book-export.test.ts > … > keeps a refusal the contract calls retryable, whatever its status
FAIL  src/books/book-export.test.ts > … > does not re-send a key whose refusal the record withholds
```

### 其余命令

```text
$ cd web/workbench && pnpm test
 Test Files  65 passed (65)
      Tests  1063 passed (1063)

$ cd /private/tmp/sn805-pr && pnpm build      # BUILD_EXIT=0
$ pnpm test:behavior
  18 passed (16.5s)

$ pnpm test:a11y
  52 passed (18.1s)

$ cd web/workbench && pnpm typecheck
$ tsc -p tsconfig.json          # exit 0

$ gh pr checks 804
18 pass / 2 skipping / 0 fail（含 "Web: lint, types, tests, build and accessibility" pass、
"Python: tests, coverage and package build" pass）

$ gh pr diff 804 --name-only
15 files（= 1 覆盖证据表 + 14 个 web/workbench/src 文件）

$ git diff origin/main...2eaed850 --stat
 15 files changed, 811 insertions(+), 69 deletions(-)
```

> 注：本机 `gh` 版本不支持 `gh pr diff --stat`，改用 `gh pr diff 804 --name-only` + `gh pr view 804 --json additions,deletions,changedFiles`
> （811 / 69 / 15）与本地 `git diff origin/main...2eaed850 --stat` 交叉核对，结果一致。

---

## 时间线

| 时间 | 动作 |
| --- | --- |
| 05:26 | `git fetch`；确认 `origin/main=3b7abd51`；从 `origin/main` 建 `review/805-panel-parity` @ `/private/tmp/sn805`；另建 detached `/private/tmp/sn805-pr` @ `2eaed850` 作为运行环境 |
| 05:27 | 读 `contracts/shared/errors.schema.json`、`contracts/workbench-openapi.yaml`（2224–2267、Response/ErrorBody 组件）、`refusal-parity.test.tsx`、`command-journal.ts` |
| 05:28 | `uv sync --frozen` + `uv pip install -e packages/contracts -e backend`；`pnpm install --frozen-lockfile` |
| 05:29 | 读工作台后端 retryable 产生点（`_error`/`_minimal_error`/`refusal_retryable`）、导出/助手 endpoint、speech `job_errors.py`；读面板与 session 全量改动 |
| 05:30 | `tests/contract/test_render_refusal_vocabulary.py` 基线 25 passed；建 `/private/tmp/805-mut` 最小沙箱，25 passed |
| 05:31 | A3 变异 ×3（面板 +码 / 面板 +可重试码 / 后端 +可重试码），分别红 1 / 1 / 3，已还原 |
| 05:32 | `pnpm test` 基线（PR head）1063 / 65 files |
| 05:33 | B4：`RecordedRefusal` 加成员 → `pnpm typecheck` 红（TS2741 + 3×TS2366），已还原 |
| 05:34 | C6 反例 ×3：红 4 / 8 / 5（含文件与用例名），逐次还原 |
| 05:35 | C7 探针（5xx/408/429/422 × 两个 session）7/7 passed，用完即删 |
| 05:36 | `pnpm build` + `pnpm test:behavior` 18 passed；`pnpm test:a11y` 52 passed（不与 behavior 并发） |
| 05:38 | 切到 `3b7abd51` 复跑基线：`pnpm test` 1043 / 64 files；`pnpm build && pnpm test:behavior` 18 passed；切回 `2eaed850` |
| 05:40 | 覆盖表重派生复核：`--write` 仅差 `audited_head`；默认检查 findings=[]；`tests/unit/test_operation_coverage.py` 43 passed |
| 05:42 | CI 复核：`gh pr checks 804` 全绿；首轮 run `43640344695` 的 Python job 日志确认 `tests_drift`；`gh pr diff 804` 与本地 diff 交叉核对 |
| 05:44 | 范围复核：无 lockfile / contracts / backend / CI / scripts / tests 改动；journal 记录形状未变；无遗留 `state.rejected` 引用 |
| 05:48 | 写本报告；在 `review/805-panel-parity` 上提交并推送，开审查 PR（标题 `review: 三面板重放口径收口的非作者审查`） |

---

## 授权与边界

- 未修改任何产品代码/测试；三个方向的变异全部在临时 worktree / 临时沙箱内完成并还原（`git status` 干净），未 push 变异。
- 未覆盖任何既有 `docs/reviews/` 文件；本文件名在 `origin/main` 上不存在（`docs/reviews/` 现有 51 个文件，无 `2026-11-15-panel-parity.md`）。
- 审查分支基于 `origin/main`，`git diff origin/main...HEAD --stat` 只含本报告。
- 未合并 PR、未 push `main`。
