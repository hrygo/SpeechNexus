# PR #797（`fix/794-settled-replay`）非作者审查：终态拒绝后的重放可用性

审查对象：`#794` 的修法 PR，head `50dad14c423c25a1a24d26d27523131c0483055b`，三个提交
（`88d1c955` 修 → `38cd3d81` 规格 → `50dad14c` 抽模块）。基线取 `origin/main` = `d55eebb4`
（`#790` 的浏览器规格 PR #793），`git merge-base origin/main 50dad14c` 恰为 `d55eebb4`，
即本分支确实从 `origin/main` 拉出、未夹带其它提交。

本次审查在工作树 `/private/tmp/sn799`（分支 `review/799-settled-replay`，基于 `origin/main`）
进行；被审代码用 `git show 50dad14c:<path>` 读取，测试在 `/private/tmp/sn799-pr`
（detached `50dad14c`）与变异副本 `/private/tmp/sn799-mut`（detached `50dad14c`）执行，
交付分支上只新增本报告。报告文件名先核过：`origin/main` 的 `docs/reviews/` 只有
`2026-11-15-coverage-evidence.md`，本文件名不存在，未覆盖任何既有审查记录。

## 结论

**修法方向正确，A1 的有意偏离成立（同样只成立于同一标签页内），但 PR 自报的「两处面板对齐」
只有终态子集成立，且新增两处必须修的语义/文案缺陷。**

- 终态拒绝（415 / 403 / `idempotency_conflict` / 各类 422 处置）不再提供可用的「按原键重放」，
  旁注按事实改写，`execute` 动作本身也拒绝已结算记录——这一层核对通过。
- 「429 与映射码 `render_submission_cleanup_pending` 可重试、故继续可用」与 `#755` 的分类、
  `#793` 已合并规格的断言一致，偏离成立；但**该偏离被 journal 的单比特形状在重载后抹掉**：
  重载后 429/清理等待既失去重放，又出现「服务端已确定性拒绝……同一次请求再发送不会改变结果」
  这句对可重试拒绝不成立的话（实测见 §C-重载探针）。
- 「按文件任务面板对齐」在**记录形状**上不成立：两处 journal 字段不同，`rejected` 与
  `settled` **不是同一位**（`rejected` ⊋ `settled`），因此两处对 429/清理等待、以及未映射裸码
  `execution_cleanup_pending` 的结论仍然相反（实测见 §B）。
- 作者对断言独立性的自述（反例 2 删掉等式后 16 例仍绿，叠加反例 1 仍全绿）被独立复现，
  这一条属实；浏览器规格的「强制点击」经实测**点不动** disabled 控件，因此「点击后请求数仍为 1」
  是 `toBeDisabled()` 的推论，不能证明应用侧守卫有效（实测见 §C-强制点击探针）。

## 逐条判定

### A1 「429 与映射码 cleanup_pending 可重试、故继续可用」这个偏离是否成立

**成立**（在 `#755` 的分类与 `#793` 规格断言的意义上），但只成立于同一标签页内。

证据链，四层互相独立：

1. 消费侧分类：`render-refusals.ts:62` `RENDER_REFUSAL_RETRYABLE` 恰为
   `["render_submission_rate_limited", "render_submission_cleanup_pending"]`，
   `isRetryableRefusal`（`:68`）只有这两个为真。
2. 生产侧同一份表述：`backend/.../interfaces/http_render_units.py` 的
   `_RETRYABLE_REFUSAL_CODES` 是同一集合，且注释写明「`execution_cleanup_pending` 的上游
   `retryable` 是另一个问题，不矛盾」；`contracts/workbench-openapi.yaml:2209` 起把
   `render_submission_cleanup_pending` 定义为「wait for the cleanup, then retry」，可重试。
3. 文案下一步是**同一次派发**，不是「派发新意图」：`render_refusals.ts:111`
   `render_submission_rate_limited` 写「等服务端要求的时间后**重新派发这次生成**」；
   `:101` `render_submission_cleanup_pending` 写「等这一次清理完成后再派发；这里可重试，
   **不需要换键**」。两处都没有要求换新键，所以「保留原键重放」就是它们自己写出的下一步，
   不存在「先等再派发新意图」这种反向读法。
4. `#755` 的 AC 明确：「工作台侧：每类给出可执行下一步；**不得**把 409 的「清理中」当 terminal
   拒绝（它是可重试的）」。`#793`（已合并 `d55eebb4`）的浏览器规格对此**已经断言过同键重发**：
   `tests/web/render-refusal.behavior.spec.mjs:336` 对 429
   `await expect(replay(units)).toBeEnabled()`，点击后 `:348-352` 断言
   `generations` 从 1 变 2、`generations[1] === generations[0]`（同一 `Idempotency-Key`）、
   `executions` 仍为一个；`:487` 起的 `execution_cleanup_pending` 用例在 `:525` 同样断言可用，
   随后断言同键重发（「the retry reuses the original key」）。所以本 PR 若把这两类也禁用，才会与 `#755`/`#793` 冲突；
   现在的处理是保留可用，**偏离成立**。

**成立的范围**：同一标签页（`recovery.retryable` 是会话内标志）。重载后不成立，见
§「重载探针」，且这是本卡必须修项 1。

### B 「渲染面板漏了，不是语义差异」「按文件任务面板对齐」：记录形状是否真等价

**不等价。** 两处 journal 是两套不同记录，`settled` 与 `rejected` 不是同一位。

逐字段对照（文件任务面板 `file-workflow/task-journal.ts` `TaskIdentity` /
渲染面板 `recovery/command-journal.ts:3` `PendingRecord`）：

| 语义问题 | 文件任务面板 | 渲染面板（本 PR） | 是否等价 |
| --- | --- | --- | --- |
| 是否保留了原意图 | `pending: "create" \| "cancel" \| null` | `command: UnitCommand \| null` | 是（`pending !== null` ⟷ `command !== null`） |
| 「服务端已拒绝」这一位 | `settled?: "terminal" \| "unreplayable"`，只在终态拒绝时写入 | `rejected: boolean`，**任何 4xx（除 408）都写入**，429 也写（`RenderUnitPanel.tsx:537-541` 注释明说「A 429 belongs here too」） | **否**：`rejected` ⊋ `settled`，前者是「观察到一次 4xx 拒绝」，后者是「意图已花掉」 |
| 是否区分可重试 | 靠 `settled` 缺省：429/清理等待落 `intent-kept` 分支（`file-task-session.ts:868`），**不写** `settled`，重载后 `canReplay` 仍为真 | 靠会话内 `retryable`（`RenderUnitPanel.tsx:557`）补偿，重载即失，按 `settled` 读 | **否**：同一 429 的重载结论相反 |
| 已识别的执行身份 | `operationId: string \| null`，`canReplay` 的第三个合取项 | 无此字段（journal 不存执行身份） | 否：渲染面板少了这一维收紧 |
| 可重放判定 | `canReplay = pending !== null && operationId === null && settled === undefined`（`file-task-session.ts:559`） | `intent ∈ {unknown, kept}`，即 `command !== null && !rejected`，再加上会话内 `retryable` | 终态子集等价，其余不等价 |

实测三条，证实「表面一致」：

1. **重载后的 429**：渲染面板 会话内 `disabled=false`（kept 旁注）→ 重载后 `disabled=true` 且
   旁注变成 `settled` 那句；同一棵树上的基线（`origin/main`）重载 429 是 `disabled=false`
   但旁注写「结果未知时不能更换意图」。
2. **重载后的映射码 cleanup_pending**：会话内 `disabled=false` → 重载后 `disabled=true`；
   文件任务面板对 429 的重载仍是可重放（`intent-kept` 不持久化 `settled`）。
3. **未映射裸码 `execution_cleanup_pending`**：渲染面板 `disabled=true` 且旁注断言
   「服务端已确定性拒绝这次派发，**没有创建作业**」；而该码的语义是「该单元的上一次执行还在清理」，
   即 Job **已存在**；文件任务面板对同一码（create 路径）有专门规则
   （`file-task-session.ts:112-122`）：`unknown-outcome`，**保留意图、不清算**，理由写得正是
   「它说不出这次意图是否被准入」。同一产品、同一机器码，两处结论相反。

所以：**「渲染面板漏了」对终态拒绝这个缺陷成立**（文件任务面板对终态确实是禁用的，渲染面板此前
没读 `record.rejected`）；但「因此按文件任务面板对齐、不需要产品决定」只在终态子集成立，
可重试子集与未映射裸码是产品决定，且本 PR 选的那一侧与文件任务面板不同——这一点 PR 描述里
只把 429 当成「有意偏离」说清了一半（会话内），没有说清重载与裸码两处。

### C3 断言独立性：作者的诚实判定被独立复现

三步，全部在 `/private/tmp/sn799-mut`（detached `50dad14c`，与 PR head 同树）：

1. 变异 1：`RenderUnitPanel.tsx:788` 的
   `disabled={busy || intent === null || !RECOVERED_INTENT[intent].replay}` 改回 `disabled={busy}`
   → `vitest run src/books/RenderUnitPanel.test.tsx`：**15 failed | 67 passed**（14 个终态码用例
   + 重载用例），与作者自报一致。
2. 变异 2：删掉 `RenderUnitPanel.test.tsx:1184` 的
   `expect(replay.disabled, ...).toBe(!step.includes("可重试"))`
   → **82 passed**（16 个 `it.each` 用例全绿）。
3. 变异 1+2 叠加 → **1 failed | 81 passed**，唯一红的是重载用例
   `expect(element).toBeDisabled()`。

即：删掉那条等式后，控件与文案的绑定**已经没有覆盖**——循环体里只剩
「文案 ↔ 分类」（`step.includes("可重试") === isRetryableRefusal(code)`）和旁注标签等式，
两者都读产品自己的表，钉不住控件。作者在 PR 描述里对这一点的自述属实。

### C4 浏览器规格的「强制点击」是否真能证明请求数不变

**不能。** `page.click({force:true})` 对 disabled 控件不会抛错，但**也点不动**：浏览器在
disabled 表单控件上根本不派发 click 事件，所以「点击后请求数仍为 1」是 `toBeDisabled()`
的推论，不是应用侧行为的证据。

探针（`@playwright/test` 自带 chromium，页面为 data URL，按钮各自带点击计数与 fetch）：

```json
{
  "forceDisabled": "no-throw",
  "clicksAfterForceDisabled": 0,
  "sawHandlerOnDisabled": 0,
  "plainDisabled": "threw: locator.click: Timeout 800ms exceeded.",
  "clicksAfterForceEnabled": 1,
  "observedRequests": ["https://example.test/allowed"]
}
```

`force: true` 跳过的是 actionability 等待（含 enabled 检查），不是浏览器的激活规则；
加 `force` 之后 disabled 按钮仍 0 次点击、0 个请求，而 enabled 对照按钮 1 次点击、1 个请求。

在变异 1 的构建上跑作者点名的三条规格，失败点也证明同一件事：三条都在
`settledReplayIsUnavailable` 的**第一行** `expect(replay(units)).toBeDisabled()`
（`render-refusal.behavior.spec.mjs:227`）就红了，**从未执行到请求计数断言**；
所以这条规格真正在工作的是 `toBeDisabled()`，强制点击 + 静默窗口那一半在变异下不承担判定。

作者在规格注释里写的是「what is asserted is the browser refusing activation on a disabled
control, not the wait」——这个描述本身准确；但 PR 描述把它当作「更硬的一层」是过强的读法：
应用侧动作守卫（`RenderUnitPanel.tsx:453`）在浏览器层**没有任何证据**，只在 jsdom 里被
`reloaded.disabled = false; fireEvent.click(...)` 覆盖（`RenderUnitPanel.test.tsx:1227`）。

### D5 范围

**通过。** `git diff origin/main...50dad14c --stat` 只有四个文件：
`tests/web/render-refusal.behavior.spec.mjs`、`web/workbench/src/books/RenderUnitPanel.test.tsx`、
`web/workbench/src/books/RenderUnitPanel.tsx`、`web/workbench/src/books/recovered-intent.ts`
（+323/−25）。`render-refusals.ts`、`recovery/command-journal.ts`、契约、后端映射、session 层
均未改；三个提交逐个看也都在这个范围内。

### D6 例数与 CI

**通过，与自报一致。**

| 项 | 自报 | 实测 |
| --- | --- | --- |
| `cd web/workbench && pnpm test` | 1027 / 64 files（基线 1008，+19） | 1027 passed / 64 files；基线工作树（`origin/main`）1008 passed / 64 |
| `pnpm build && pnpm test:behavior` | 15 passed | 15 passed（文件任务 9 + 渲染 6） |
| `pnpm test:a11y`（behavior 之后顺序跑） | 52 passed | 52 passed |
| `pnpm typecheck` | 绿 | 5 个 workspace 项目全部 Done |
| `gh pr checks 797` | 全绿 | 20 pass / 2 skipping / 0 fail（PR `MERGEABLE`/`CLEAN`） |

## 必须修清单

### 1（阻塞）重载后的可重试拒绝被当成终态，并给出不成立的旁注

- 现象：同一标签页内 429 / 映射码 `render_submission_cleanup_pending` 保留重放（正确）；**刷新后**
  `recoveredIntentOf` 读到 `rejected && !retryable` → `settled`，重放按钮 `disabled`，旁注出现
  「服务端已确定性拒绝这次派发，没有创建作业：同一次请求再发送不会改变结果，所以这里禁用了按原键重放」。
- 为什么不成立：这两个拒绝**没有创建作业、键也没有花掉**（`#755` 明说可重试、`#793` 规格断言
  同键重发后成功），重载后同样是「等一会儿再发同一请求就能成功」，所以该旁注对它们事实错误；
  同时它把 `#755`/`#793` 刚区分出来的可重试类又折回终态，正是 `#755` 禁止的方向。
- 与文件任务面板相反：那边 429 走 `intent-kept`、不持久化 `settled`，重载 `canReplay` 仍为真
  （`tests/web/file-task.behavior.spec.mjs:275` 起断言可用）。
- 修法建议（任一）：
  - 正解：journal 记录这次拒绝的**类别**。现成的范例就在同仓：文件任务面板把
    `settled: "terminal" | "unreplayable"` 持久化，可重试拒绝不写该字段。
    `recovery/command-journal.ts` 是 `manuscripts`/`assistant`/`books` 三个面板共用的，
    属于 `#794` 已知边界里点名的「改记录形状」，需要在本卡或新卡里明确承担。
  - 过渡：若不改形状，至少不能断言「确定性拒绝」与「同一次请求再发送不会改变结果」，
    并且不能把「本页不区分这次拒绝是否可重试」说成已结算；同时按 `#794` AC 的要求
    把「跨面板一致」的说法收窄为「同一标签页内一致」。
- 补充：该状态不是死路（重放禁用、新派发被 `locked` 挡住，仍可「结束已拒绝单元请求」再新派发），
  所以这是「文案错误 + 丢失一个本来有效的手柄」，不是阻塞操作。

### 2（阻塞或必须在 PR/issue 里明确排除）未映射裸码 `execution_cleanup_pending` 被当作终态

- 现象：`refusedCode` 不认识这个裸码 → `problemOf` 落到 `{key:"rejected"}` → `keepsReplay` 为假
  → `settled`，旁注与告警一起断言「确定性拒绝、没有创建作业」。
- 为什么不成立：该码的语义是「该单元的上一次执行还在清理」（Job 已存在），
  `#755` 明确「不得把清理中当 terminal」，文件任务面板对 create 路径的同名码专门保留为
  `unknown-outcome`。PR 自报的可用性表把这一行（`false→true`、`1→1`）当成「对齐」的验证，
  实际上它是通用拒绝路径的副产品，方向与两处既有判断都相反。
- 可达性有限：生产入口把该码映射成 `render_submission_cleanup_pending`
  （`http_render_units.py` `_CONFLICT_CODES`，且该上游码被标为 `_CONFLICT_CODES_RESERVED`），
  所以这是「映射不一致时才出现」的路径，不是今天的常规路径；但它恰好是本卡宣称验证过的第四态，
  结论方向错了，不能作为对齐证据留下。
- 修法建议：未映射的 409（至少这个已由上游契约为「清理中」的码）按 `unknown-outcome` 处理，
  与文件任务面板一致；或至少不要用「没有创建作业」这句在本路径上断言事实。

### 3（建议，非阻塞）浏览器规格补一条真能点到的动作守卫用例

`toBeDisabled()` + 浏览器拒激活已经覆盖了「控件不可用」；但 `#794` 的实质是「文案说无效的动作
不得可用」，动作守卫（`RenderUnitPanel.tsx:453`）目前只有 jsdom 证据。把 jsdom 重载用例里的做法
搬到浏览器：`page.evaluate` 摘掉 `disabled` 后点击，断言 `generations` 仍为 1，即可让「更硬的一层」
名副其实。

### 4（建议）等式断言的注释与去留

`RenderUnitPanel.test.tsx:1184` 这条等式一旦被删就无覆盖（本审查已复现）。它是当前唯一把
控件绑到文案上的断言，建议保留并在注释里写明「删掉它则 15 红退化为 1 红」，避免后续维护者
按「与分类断言重复」的理由清理掉。

## 只能依赖作者陈述的点

- 「改动前后各跑一次真实浏览器 preview、得到四种终态的 `disabled` 与请求计数」：本审查复核了
  改动后的三态（415/403/`idempotency_conflict` 的 3 红、429 与 cleanup 保持可用、裸码禁用），
  但**改动前**的那一列数字（`1 → 2`）只在 `#794` 的缺陷描述与 PR 正文里，未独立重跑基线浏览器套件。
- 「`pnpm lint`、`pnpm format:check`、根 `pnpm test`（admin 151）」：本次只跑了
  workbench 单测、build、behavior、a11y、typecheck 与 `uv run --no-project ruff format --check .`，
  未独立重跑根 `pnpm test` 的 admin 分流与 lint/format 门禁（CI 的 `Web: lint, types, tests,
  build and accessibility` 与 `Scope routing and CI / design-token checks` 均为 pass，见 D6）。
- 「journal 只持久化一个拒绝位」：这一点经代码与重载探针核实（`PendingRecord` 只有
  `uncertain`/`rejected`），不是纯陈述。

## 过程记录（时间线）

1. `git fetch`；确认 `origin/main = d55eebb4`、目标提交 `50dad14c` 存在。
2. 查 `docs/reviews/`：`origin/main` 48 个文件，`2026-11-15-settled-replay.md` 不存在。
3. `git worktree add /private/tmp/sn799 -b review/799-settled-replay origin/main`；
   `git merge-base origin/main 50dad14c` = `d55eebb4`。
4. `git diff origin/main...50dad14c --stat`（4 文件）；`git show 50dad14c:...` 读四个改动文件与
   `render-refusals.ts`、`command-journal.ts`、`task-journal.ts`、`file-task-session.ts`。
5. 基线测试（审查工作树）：`pnpm install --frozen-lockfile` 后 `cd web/workbench && pnpm test`
   → 1008 passed / 64 files。
6. 新建 detached 副本 `/private/tmp/sn799-pr`（`50dad14c`）：install → `pnpm test`
   → 1027 passed / 64 files；`pnpm build` → `pnpm test:behavior` 15 passed；
   `pnpm test:a11y` 52 passed（顺序执行，未并发）；`pnpm typecheck` 全绿；
   `gh pr checks 797` 20 pass / 2 skipping。
7. 新建变异副本 `/private/tmp/sn799-mut`（detached `50dad14c`，真实 `pnpm install`）：
   变异 1 → 15 red；恢复后变异 2 → 82 green；变异 1+2 → 仅重载用例 red；
   变异 1 后 `pnpm build` + `playwright test -g "415|403|idempotency_conflict"` → 3 red，
   全部卡在 `toBeDisabled()`。
8. 强制点击探针（chromium/data URL）：见 C4。
9. 重载探针（在副本里临时加 `PROBE` 用例，跑完 `git checkout --` 复原，两棵副本树最终
   `git status` 均干净）：429 与 mapping cleanup 的会话内/重载、裸码、以及基线重载 429 四组输出
   见 §B/§C。
10. 写本报告；`uv run --no-project ruff format --check .` → 1862 files already formatted。

## 实测命令与输出（摘录）

工作树基线（`/private/tmp/sn799`，`origin/main` 内容）：

```
 Test Files  64 passed (64)
      Tests  1008 passed (1008)
```

PR 副本（`/private/tmp/sn799-pr`，`50dad14c`）：

```
      Tests  1027 passed (1027)
  Test Files  64 passed (64)
...
  15 passed (9.9s)          # pnpm build && pnpm test:behavior
  52 passed (18.8s)         # pnpm test:a11y
# typecheck: web/shared, packages/contracts, web/admin, web/workbench 全部 Done
```

`gh pr checks 797`：20 项 pass，2 项 skipping（`Docs: markdown code block formatting`、
`Node Controller image evidence / Build and probe`），0 项 fail。

变异复现（`/private/tmp/sn799-mut`）：

```
# 变异 1：disabled={busy}
     × keeps the original-key replay after render_submission_text_too_long exactly when its copy promises a retry
     ... （14 个终态码用例）
     × keeps a settled refusal unreplayable after a reload and ends it explicitly
 Test Files  1 failed (1)
      Tests  15 failed | 67 passed (82)

# 变异 2：删掉 expect(replay.disabled, ...).toBe(!step.includes("可重试"))
 Test Files  1 passed (1)
      Tests  82 passed (82)

# 变异 1+2
     × keeps a settled refusal unreplayable after a reload and ends it explicitly
 Test Files  1 failed (1)
      Tests  1 failed | 81 passed (82)
```

变异 1 的浏览器规格（只跑作者点名的三条）：

```
    - Expect "toBeDisabled" ... with timeout 5000ms
      - locator resolved to <button type="button">按原键重放单元请求</button>
        - unexpected value "enabled"
  > 227 |   await expect(replay(units)).toBeDisabled();
  3 failed
    tests/web/render-refusal.behavior.spec.mjs:378:1 › 409 idempotency_conflict：…
    tests/web/render-refusal.behavior.spec.mjs:415:1 › 415：提示指向媒体类型配置，不提供重试
    tests/web/render-refusal.behavior.spec.mjs:452:1 › 403：权限提示在真实布局里可见且不提供重试
```

重载/裸码探针（PR 副本，`vitest -t PROBE`）：

```
PROBE-INSESSION-429 disabled= false kept-note= true
PROBE-RELOAD-429 disabled= true
PROBE-RELOAD-429 settled-note= true
PROBE-RELOAD-429 aside= 原请求已保存在此标签页。服务端已确定性拒绝这次派发，没有创建作业：同一次请求再发送不会改变结果，所以这里禁用了按原键重放。用「结束已拒绝单元请求」明确结束它，再按那次拒绝的处置说明重新开始。
PROBE-RELOAD-429 alert= undefined
PROBE-BARE-CLEANUP disabled= true
PROBE-BARE-CLEANUP settled-note= true
PROBE-BARE-CLEANUP aside= （同上 settled 句）
PROBE-MAPPED-CLEANUP-IN disabled= false
PROBE-MAPPED-CLEANUP-RELOAD disabled= true
```

基线对照（`/private/tmp/sn799`，`origin/main` 内容）：

```
PROBE-BASE-INSESSION disabled= false
PROBE-BASE-RELOAD disabled= false
PROBE-BASE-RELOAD aside= 原请求已保存在此标签页。不会自动重放，结果未知时不能更换意图。
```

强制点击探针：

```
{
  "forceDisabled": "no-throw",
  "clicksAfterForceDisabled": 0,
  "sawHandlerOnDisabled": 0,
  "plainDisabled": "threw: locator.click: Timeout 800ms exceeded.",
  "clicksAfterForceEnabled": 1,
  "observedRequests": ["https://example.test/allowed"]
}
```

文档格式门禁：`uv run --no-project ruff format --check .` → `1862 files already formatted`。
