# PR #801（`fix/798-journal-category`）非作者审查：journal 持久化拒绝类别

审查对象：`#798` 的修法 PR，head `b33bf2cf3f01803c6ec4fa30ffb7cfdbb9215bab`，分支
`fix/798-journal-category`，修 `#794`（`d6b9ef5e`）留下的「journal 只存一个拒绝位、重载后
分不清终态与可重试」过渡态。基线取 `origin/main` = `22bd4142`（`#792` 的 PR #796），
`git merge-base origin/main b33bf2cf` 恰为 `22bd4142`，即被审分支确实从 `origin/main` 拉出、
未夹带其它提交。

本次审查在工作树 `/private/tmp/sn802`（分支 `review/802-journal-category`，基于
`origin/main`）进行；被审代码用 `git show b33bf2cf:<path>` 读取；测试在 detached 副本
`/private/tmp/sn802-run`（`b33bf2cf`）执行，变异只在该副本内就地施加、跑完即 `git checkout`
复原，未 push、未进入交付分支。交付分支上只新增本报告（`git diff origin/main...HEAD --stat`
只有本文件）。报告文件名先核过：`origin/main` 的 `docs/reviews/` 有 50 个文件，
`2026-11-15-journal-category.md` 不存在，未覆盖任何既有审查记录。

## 结论

**修法方向正确——它就是 `#797` 审查 MF-1 点名的「正解：journal 记录这次拒绝的类别」，
核心语义经代码、探针与变异复核全部成立；但回滚（降级）方向不可读这一点没有写进代码，
且新增的 415 刷新规格里「强制点击 + 请求数仍为 1」实测是空断言。**

- **记录形状**：`rejected: boolean` → `refused: "retryable" | "terminal" | "unclassified" | null`，
  码表仍留在 `books/render-refusals.ts`，journal 只存语义分类——**成立**，两处表没有第二份副本。
- **旧记录读法**（`rejected:true` → `unclassified`，`rejected:false` → `null`）**成立且确定**；
  旧形状只在 `#parse(..., allowLegacy=true)`（构造函数，`command-journal.ts:99`）被接受，
  `save` 走 `#parse(record, false)`（`:148`），**只有两处调用点**，没有第三处。
- **A1 的实质问题（会不会让用户永久失去一个本来可重放的键）**：**不会失去「能继续」的路**。
  旧记录里的 429 升级后变成 `unclassified` → 重放禁用（保守方向），但 `recorded` 状态带
  `dismiss: true`，「结束已拒绝单元请求」清掉记录后 `locked` 解除（`RenderUnitPanel.tsx:381`），
  可以按新键重新派发。而且这类可重试拒绝（429 / 清理等待）**没有为该键创建过 Job**，
  新键与旧键在服务端等价，所以这里的损失是「那一个键的复用性」，不是「一次操作」。
  这一点与作者陈述一致，也是我独立核过的最关键一条。
- **A2 的实质问题（回滚）**：`version` 保持 `1`，而**旧包读新记录一律抛错**——实测五条形状
  （`retryable` / `terminal` / `unclassified` / `refused:null` / 空记录）全部
  `Invalid private recovery record`，**连 `{command:null,uncertain:false,refused:null}` 这条
  空记录都读不了**。三个消费者都是 `catch → blocked:true`，于是面板显示
  「恢复信息未能保存，已暂停提交」并 `locked`，且**坏记录不会被删除**，同一标签页每次刷新都
  重犯，直到关掉标签页（`sessionStorage` 是标签页作用域）。这是 fail-closed 的**响亮失败**，
  不是静默丢信息、也不是误判可重放——按本卡的口径，这一条的最低要求是「在代码里写明回滚不兼容」，
  而这句现在是缺的（见 MF-1）。
- **反例**：三条假设独立复现，jsdom **27 / 3 / 6** 与自报数字**完全一致**，红点位置也一致；
  浏览器层两条刷新用例在 M1、M3 下分别转红，且 M3 的红点落在**重载之后**的断言行
  （`:643` / `:684`），说明新规格真的在测量重挂载后的状态（见 §C5/§C6）。
- **例数与 CI**：`pnpm test` 1043 / 64 files（基线 1030 / 64，+13）、`test:behavior` 18
  （基线 16，+2）、`test:a11y` 52、`typecheck` 5 个项目全 Done、`gh pr checks 801`
  20 pass / 2 skipping / 0 fail——逐项独立复现（见 §D8）。
- **必须修两处**：①回滚不兼容未在代码里交代（文档级，不改行为也安全）；②新增 415 刷新规格的
  请求计数断言与 jsdom 的「defence in depth」都是空转，重载后终态的动作守卫在浏览器层没有
  有效证据——这是 `#797` 审查 MF-3 的同类问题**再次出现**（见 MF-2）。

## 逐条判定

### A1 旧记录读成 `unclassified`（按终态行为）是否安全

**安全。** 三个子问题分开答：

1. **旧代码里 `rejected:true` 包含 429（可重试）**，所以升级前遇到 429 的用户，升级后重载会看到
   「不可重放」。这是保守方向，但它**不会让用户走进死路**，三道证据：
   - `RECOVERED_INTENT.recorded = { replay: false, dismiss: true }`
     （`recovered-intent.ts:58-61`），「结束已拒绝单元请求」始终可用；
   - `dismissRejected`（`RenderUnitPanel.tsx:596`）只要求 `recovery.refused !== null`，
     `journal.clear()` 后 `pending: null` → `locked = busy || pending !== null || blocked`
     （`:381`）解除 → 单元按钮恢复可派发；
   - 该状态**不是**「结果未知」：`rejected` 位只在**观察到 4xx 应答**（除 408）时写入
     （`RenderUnitPanel.tsx:552-556`），所以旧记录里不存在「Job 可能已创建」的情形；
     429 与清理等待的语义本身就是「服务端没有为这次派发创建 Job」，因此新键重新派发不会造成
     重复提交。这两点在 `render-refusals.ts` 的 next-step 文案与 `#755` 的分类里是一致的。
   jsdom 用例 `keeps a record written before #798 unreplayable, and says only what it holds`
   把这条路径完整走了一遍（禁用 → 摘 `disabled` 后点击仍不发送 → dismiss → 旁注与按钮消失），
   我本地实跑通过。
2. **有没有路径把 `unclassified` 写回磁盘**：**有，且这正是「下次保存即迁移」**。实测（探针，
   见「实测输出」P1–P3）：
   - 构造/restore **不重写字节**：旧记录读入后 `sessionStorage` 仍是原来那串 `rejected` 文本；
   - 之后任何一次 `save`（`save(load())`、`execute()` 里的 `journal.save(journal.load())`、
     `dismiss` 走 `clear()`）都会写出当前形状。`save(load())` 实测写出
     `{"version":1,…,"refused":"unclassified"}`；`clear()` 写出 `refused:null`（空记录，不是
     `unclassified`）。
   - 关键在于**值没有变得更坏**：旧记录在内存里本来就只能读成 `unclassified`，写下去是同一个值，
     不存在「本可恢复的信息被压掉」。所以这不是缺陷；但它在语义上意味着 **`unclassified` 一旦落盘
     就永久固化**（没有任何路径能再升级成 `retryable`/`terminal`），建议在 `RecordedRefusal` 的注释
     里补一句「unclassified 不可再造」（见建议 1）。
3. **`rejected:false` → `null`** 正确，且构造函数随后按既有规则把「有 command 且无拒绝」判为
   `uncertain`（`command-journal.ts:102`），与改前 `if (command && !rejected) uncertain = true`
   逐字等价（探针 P5：`rejected:false` 读成 `{uncertain:true, refused:null}`）。

### A2 `version` 保持 `1` 是否正确（回滚场景）

**升级方向正确，降级方向不可用，且这一点没有写在代码里——必须补文档（MF-1），不必改行为。**

实测（探针 P4，造含 `refused` 的记录交给 `origin/main` 的旧解析器）：

```text
[retryable]     => threw: Invalid private recovery record   bytes untouched: true
[terminal]      => threw: Invalid private recovery record   bytes untouched: true
[unclassified]  => threw: Invalid private recovery record   bytes untouched: true
[refused:null]  => threw: Invalid private recovery record   bytes untouched: true
[empty record]  => threw: Invalid private recovery record   bytes untouched: true
```

机理：旧 `#validate` 用 `fields(input, ["version","command","uncertain","rejected"])`，而
`fields` 要求键数与键名**全对**（4 键且都有）——新记录键数同为 4，但缺 `rejected`，所以直接抛
`Invalid private recovery record`。**连新包写的空记录也读不了**，也就是说任何一次 `begin` /
`failure` / `clear` 之后的 `sessionStorage` 对旧包都是毒记录，不是只有「拒绝过的记录」才有问题。

后果链（三个消费者都是这个形状，改前改后相同）：

| 消费者 | 构造期失败后的状态 | 用户可见结果 |
| --- | --- | --- |
| `books/RenderUnitPanel` | `{journal:null, pending:null, refused:null, blocked:true}`（`:366` catch） | 旁注换成 `problems.storage`「恢复信息未能保存，已暂停提交。请修复浏览器存储后重试。」，`locked` 让表单与单元按钮全部禁用 |
| `books/book-export.ts` | `{blocked:true, problem:"storage"}`（`:85`） | 同上文案；无旁注（`pending` 仍为 `null`） |
| `assistant/assistant-session.ts` | `{blocked:true, problem:"storage"}`（`:96`） | 同上文案 |

严重度评估：**不是静默丢信息，也不是误判可重放**——它响亮失败并 fail-closed，且
`sessionStorage` 是标签页作用域，**关掉标签页即自愈**（新标签页拿到空记录）。所以我不把
「version 必须改成 2」当作修法要求（改成 2 也不会改变旧包行为：形状检查先失败），但**代码注释
只写了升级方向**，见 MF-1。

### B3 `keepsReplay(disposition)` 是否唯一判断

**是。** 三处核对：

1. **`keepsReplay` 只剩一个用途**：`RenderUnitPanel.tsx:157` 定义，全仓只在 `:571` 被调用，
   即把这次应答的分类折算成要落库的 `category`；面板的**可用性**不再读它，而读
   `recoveredIntentOf(recovery)`（`:382`）→ `RECOVERED_INTENT[intent].replay`（`:811`），
   动作守卫也读同一个 `intent`（`:469`）。所以「重放是否可用」只有一个判据。
   `keepsReplay` 对 refusal 分支直接委托 `isRetryableRefusal(problem.code)`
   （`render-refusals.ts:68`，集合恰为 429 与映射码 cleanup），没有第二份分类表。
2. **`ObservedAnswer` / `answer` 这第二套判断确已删除**：新树 `git grep ObservedAnswer` 只命中
   `recovered-intent.ts` 的历史注释，`recoveredIntentOf` 的入参已从
   `{pending, rejected, answer, blocked}` 收成 `{pending, refused, blocked}`（`:82-98`），
   面板 `recovery` 状态里也没有 `answer` 字段。改前「会话内 `answer` 覆盖 journal 位」的表达式
   整段消失。
3. **三个消费者统一读 `refused`**，`journal.failure(...)` 全仓只有三处调用且都已改：
   `RenderUnitPanel.tsx:575`（写 `retryable|terminal`）、`book-export.ts:181`、
   `assistant-session.ts:238`（后两处写 `unclassified`）。后两者的 `rejected` 仍是
   `record.refused !== null`（`book-export.ts:81,185`、`assistant-session.ts:92,242`），
   只用于告警与「结束…请求」按钮，**不构成重发判据**（这两个面板的重放按钮
   `disabled={state.busy}`，改前也是如此，本 PR 未改这两个面板）。
4. **`manuscripts` 确实不被共用**（作者对任务卡的事实纠正是对的）：
   `manuscripts/editor-journal.ts` 的 `EditorRecord` 字段为
   `version/forms/project/view/draft/pending`，没有拒绝位，也不 import `CommandJournal`。
   真实消费者就是本 PR 改的这三处。（另注：`file-workflow` 有自己的一套
   `rejected`/`settled`/`unreplayable`，但那是 `task-journal` 的独立记录，不属于
   `CommandJournal` 的爆炸半径；`isTerminal(status)` 判的是 **Job 状态**终态，不是「键能否重发」。）

### B4 `unclassified` 是否被文案说成「确定性拒绝」

**没有，作者的说法属实（实测）。** 四段旁注的实测文本（`recovered-intent.ts:42-63`）：

- `recorded`（`unclassified`）：**「原请求已保存在此标签页。这条记录只说明服务端拒绝过这次派发，
  没有保存该拒绝是否可重试，因此本页不提供按原键重放。请按那次拒绝的处置说明处理，或用
  「结束已拒绝单元请求」结束它后重新派发。」** —— 只陈述「拒绝过」，没有任何确定性判断。
- `settled`（`terminal`）才写「服务端已确定性拒绝这次派发：同一次请求再发送不会改变结果……」。
- 浏览器层实证：M1 变异（分类不落库）下，415 刷新用例失败的正是 `:684`
  `toContainText("服务端已确定性拒绝这次派发")`，实际渲染出的是 `recorded` 那句
  「这条记录只说明服务端拒绝过这次派发，没有保存该拒绝是否可重试」——即 `unclassified` 确实
  走 `recorded` 文案，没有被冒充成终态。
- 另外两处相关文案也没有越界：`kept` 改成「该拒绝可重试：下一步就是等待后用原键重新派发」，
  与 `render-refusals.ts` 的 next-step 一致；`assistant` / `book-export` 的 `rejected` 文案
  （「请求被拒绝。请核对输入后重试。」／「本次导出请求已被拒绝。请核对导出格式与选择范围后重试。」）
  不含确定性断言。

### C5 三条反例是否独立复现

**三条全部复现，且 jsdom 数字与自报完全一致**（在 `/private/tmp/sn802-run`，每次一条 hunk，
跑完立即复原）：

| 反例（我自建的等价变异） | 具体改动 | jsdom 红 | 与自报 |
| --- | --- | --- | --- |
| M1 丢掉持久化的分类（记录退回单比特语义） | `failure()` 内 `const refused = old.uncertain \|\| refusal === null ? null : "unclassified";` | **27 failed** / 103（两个套件） | 27 ✅ |
| M2 旧记录读成 `retryable` | `#parse` 中 `refused = value.rejected ? "retryable" : null;` | **3 failed** | 3 ✅ |
| M3 重载后仍压成 `unclassified` | 面板恢复分支 `refused: record.refused === null ? null : "unclassified"` | **6 failed** | 6 ✅ |

红点位置也与描述相符：

- M1 的 27 红 = journal 层 3 条（终态/可重试落库、未知优先）+ 面板 `it.each` 6 行全部
  （含终态行，因为落库分类一丢，终态也变成 `unclassified`）+ 16 个「按码 × 分类」用例
  + 裸码用例 1 条 + 「会话内终态不可重放」1 条；这正好证明**新字段是这些断言的锚**，不是装饰。
- M2 的 3 红是：`reads a record written before #798 as an unclassified refusal, deterministically`、
  `migrates a restored legacy record …`、面板 `keeps a record written before #798 unreplayable
  …`——即「旧记录」这条迁移假设有独立断言，不是顺带。
- M3 的 6 红**全部**是 `it.each` 的 6 行（3 个可重试 + 3 个终态），说明「重载前后一致」这张表
  是逐行独立的。

### C6 两条新浏览器规格是否真的在重挂载后点击并计数

**429 规格成立；415 规格的请求计数那一半是空断言。**

- **429（`render-refusal.behavior.spec.mjs:618`）**：`page.reload()` → `openRenderUnits(page)`
  → 先断言 `replay(reloaded)` 可用、旁注含「该拒绝可重试」「等待后用原键重新派发」、
  `reloaded.getByText("结果尚未确认")` 为 0 个、`generations` 仍为 1（重载本身不发请求）
  → **真的点**（非 force）→ 断言 `generations` 变 2、`generations[1]` 与 `generations[0]`
  **整个对象相等**（同一 `Idempotency-Key`）、`executions === [ids.job]`。
  M3 变异下它红在 `:643`（**重载之后**的 `toBeEnabled()`），证明这条规格测量的是重挂载后的状态。
- **415（`:665`）**：重载后 `toBeDisabled()` + 旁注含「服务端已确定性拒绝这次派发」且
  **不含**「无法判断该拒绝是否可重试」——这两半有效（M3 下红在 `:684`，即重载后的文案断言）；
  但随后 `replay(reloaded).click({ force: true })` + `expect(generations).toHaveLength(1)`
  **不承担任何判定**：`force` 只跳过 actionability，浏览器在 disabled 控件上根本不派发 click。
  证据是我把动作守卫（`RenderUnitPanel.tsx:469`）整行删掉（M4）后 **这条规格仍然全绿**，
  只有既有的 `415：两层守卫之下，可点的重放控件也发不出已结算的键`（`:471`）转红，
  而它红在**直接调用 React handler** 那一步（`:512`「a settled key is not re-sent even when its
  handler is invoked directly」）。这与 `#797` 审查 C4 的结论同一机理。
- 进一步：jsdom 那层 `after.disabled = false; fireEvent.click(after)`（`RenderUnitPanel.test.tsx`
  重载行）**同样是空转**——我的探针在 M4（守卫已删）下实测该路径 `generate` 调用数仍为 0，
  而 `props.onClick()` 直调时为 1。也就是说**重载后终态的动作守卫目前没有任何一层有效证据**
  （会话内的终态由 `:471` 那条规格覆盖）。见 MF-2。

### D7 改动范围与 lock 文件

**通过。** `git diff origin/main b33bf2cf --name-status` 恰 8 个文件，全部在允许范围内：

```text
M tests/web/render-refusal.behavior.spec.mjs          （浏览器规格）
M web/workbench/src/assistant/assistant-session.ts    （必要 consumers）
M web/workbench/src/books/RenderUnitPanel.test.tsx    （单测）
M web/workbench/src/books/RenderUnitPanel.tsx         （books）
M web/workbench/src/books/book-export.ts              （books）
M web/workbench/src/books/recovered-intent.ts         （books，状态表）
M web/workbench/src/recovery/command-journal.test.ts  （单测）
M web/workbench/src/recovery/command-journal.ts       （记录形状）
```

无 `pnpm-lock.yaml` / `package.json` / `pyproject.toml` / `uv.lock` 改动；未碰
`books/render-refusals.ts`（码表留在原处，符合「不复制码表」）、契约、后端映射、session 之外的层。
`+532/−139` 与自报一致。

### D8 例数、门禁与 CI

**与自报一致，逐项实测。**

| 项 | 自报 | 实测 |
| --- | --- | --- |
| `cd web/workbench && pnpm test` | 1043 / 64 files（基线 1030） | **1043 passed / 64 files**；基线工作树（`origin/main`）**1030 passed / 64 files** |
| `pnpm build && pnpm test:behavior` | 18 passed（基线 16） | **18 passed**（文件任务 9 + 渲染 9，含新增 `:618` `:665` 两条）；基线 `origin/main` **16 passed** |
| `pnpm test:a11y`（behavior 之后顺序跑，未并发） | 52 passed | **52 passed** |
| `pnpm typecheck` | 通过 | 5 个 workspace 项目全部 `Done` |
| `gh pr checks 801` | 全绿 | **20 pass / 2 skipping / 0 fail**（skipping 为 `Docs: markdown code block formatting` 与 Node Controller 构建矩阵，均按 scope 跳过） |

## 必须修清单

### MF-1（必修，文档级）回滚不兼容没有写进代码

- 现象：`PendingRecord` 的注释（`command-journal.ts:35-49`）只交代了**升级**方向
  （「a record written by the previous bundle carries `rejected` … it is *restored*」），
  对一个**降级**的包读到本包写的记录会发生什么，一个字也没有。实测旧包**对任何新形状记录都抛
  `Invalid private recovery record`（含空记录）**，三个消费者据此 `blocked:true`，面板显示
  「恢复信息未能保存，已暂停提交」并锁死，坏记录不删除、每刷新一次重犯，直到关标签页。
- 为什么不静默也不行：这是「部署回滚」这种常规操作的必然后果，而代码里没有任何提示让人知道
  「旧包读新记录会整块不可用」，下一位改这段的人会以为兼容是双向的。
- 修法：在 `PendingRecord` 的版本注释里补一段，例如

  > 降级不兼容：这个形状只有本包能读。旧包要求 `rejected` 存在，读到 `refused` 会抛
  > `Invalid private recovery record`，三个消费者都把它当成存储损坏（`blocked`），记录不会被
  > 删除，因此同一个标签页会一直不可用，直到关掉标签页。回滚部署时这是预期代价，不是 bug。
  >
  > A downgrade cannot read this: the previous bundle requires `rejected`, so it rejects every
  > record this build writes (including an empty one) as an unreadable storage record. The three
  > consumers turn that into `blocked` and never delete the record, so the tab stays unusable
  > until it is closed.

  不需要改行为（抛错是 fail-closed，且 `sessionStorage` 标签页作用域已把影响限制在单个标签页）；
  **不建议**为此把 `version` 改成 2——旧包的形状检查先失败，改版本号不改变任何行为，只会让
  「version 1 可能是旧形状」这条隐含前提失效。

### MF-2（必修，测试强度）重载后终态的动作守卫没有有效证据；新 415 规格的计数断言空转

- 现象一：`tests/web/render-refusal.behavior.spec.mjs:687` 的
  `click({ force: true })` 打在仍然 `disabled` 的控件上，浏览器不派发 click。删掉动作守卫
  （`RenderUnitPanel.tsx:469`）后该规格**仍然全绿**，唯一转红的是既有的 `:471` 规格。
- 现象二：jsdom 重载行的 `after.disabled = false; fireEvent.click(after)`
  （`RenderUnitPanel.test.tsx` 重载行末）同样到不了 handler——M4 下实测 `generate` 调用数为 0，
  直调 `props.onClick()` 才是 1。所以这行注释所称的「defence in depth / the action guard,
  not the attribute, is what refuses the key」目前不成立。
- 影响：产品行为**是对的**（守卫在，`#755`/`#794` 的会话内语义也由 `:471` 覆盖），
  但「重载后终态不可重发」这一条在行为层只有 `toBeDisabled()` 的证据，与 `#797` 审查 MF-3
  提出的要求相同、且这次又在新规格里重犯。
- 修法（任一即可，建议照 `:471` 的做法）：重载后先
  `await replay(reloaded).evaluate((b) => { b.disabled = false; })`，再
  `await replay(reloaded).click()` 断言 `generations` 仍为 1；或直接取
  `__reactProps$*` 的 `onClick` 调用一次再断言。jsdom 那行同步改成同样的直调方式
  （或删掉它并把注释收窄为「属性被摘掉也只是属性」）。

## 建议（非阻塞）

1. 在 `RecordedRefusal` 的注释里点明 **`unclassified` 一旦落盘就永久固化**：没有任何代码路径能把
   它再升级成 `retryable`/`terminal`，唯一的出路是 `dismiss`（清记录）。这是刻意的保守选择，
   但现在只有「为什么读成 `unclassified`」，没有「读了以后就没有回头路」。
2. **跨面板不一致会被这个 PR 固化下来**：`assistant` 与 `book-export` 在**任何** 4xx（除 408/429）
   之后仍提供「按原键重放…请求」（`disabled={state.busy}`，不看 `rejected`），渲染面板在同一类拒绝
   上禁用重放。本 PR 之前这三处行为就是这样，本 PR 没有改变它们（作者也如实说了「行为逐字段一致」），
   但既然类别已经落库，这两个面板写下的 `unclassified` 会永久保持「说不清」，建议后续卡统一
   两者对拒绝的动作口径，否则「journal 存了类别」只在第三处生效。
3. `refusedField` 的报错消息是 `Invalid private recovery refusal`，与同文件其余
   `Invalid private recovery record/state` 不一致；不影响行为，可选统一以便排障时按前缀过滤。

## 只能依赖作者陈述的点

- **「403 / `idempotency_conflict` 刷新后仍 disabled、请求数 1」**：这两类刷新后的**浏览器**
  行为我未独立重跑（新增的浏览器刷新规格只有 429 与 415 两条）；它们在 jsdom 的 6 行
  `it.each` 里被覆盖（且 M1/M2/M3 三个变异会把这些行打红），但「真实浏览器重载后请求数」这一层
  只有 429/415 的证据。
- **反例的具体 hunk**：作者未在 PR 里给出变异实现，我按三条假设自建等价变异，得到**完全相同的
  27 / 3 / 6**，因此认为变异落在同一等价类；但这不等于我复用了作者的补丁。
- **`pnpm lint`、`pnpm format:check`**：未独立重跑（CI 的
  `Web: lint, types, tests, build and accessibility`、`Scope routing and CI / design-token checks`
  均为 pass，见 §D8）。
- **`execution_cleanup_pending` 的可达性**：作者自承「本操作上不可达（后端口径）」，浏览器规格
  验证的是**面板映射**而非可达拒绝路径；我核对了生产入口：
  `backend/.../workbench/interfaces/http_render_units.py:88-91` 的 `_CONFLICT_CODES` 把
  `execution_cleanup_pending` 映射成 `render_submission_cleanup_pending`，且 `:100` 的
  `_CONFLICT_CODES_RESERVED` 把它标为「本次 dispatch 到达不了」，与这条自述一致；我没有去后端
  造这条路径。
- 作者对任务卡的事实纠正（`command-journal.ts` 不被 `manuscripts` 共用、真实消费者是三处）：
  **我独立核实成立**，不属于「只能依赖陈述」。

## 过程记录（时间线）

1. `git fetch`；确认 `origin/main = 22bd4142`、`b33bf2cf` 存在；`git merge-base` = `22bd4142`。
2. 查 `docs/reviews/`（`origin/main` 50 个文件）：`2026-11-15-journal-category.md` 不存在。
   注：第一眼在用户主工作树（分支 `docs/tracker-batch12`）看到该目录为空，改在
   `origin/main` 的 worktree 内复核后才是 50 个文件，未据此误判。
3. `git worktree add /private/tmp/sn802 -b review/802-journal-category origin/main`
   （HEAD `22bd4142`，与 `origin/main` 同指）。
4. `pnpm install --frozen-lockfile`（269 包，lockfile 未变）。
5. 读被审内容：`git show b33bf2cf:` 读 `command-journal.ts`、`recovered-intent.ts`、
   `RenderUnitPanel.tsx`、`book-export.ts`、`assistant-session.ts`、两个单测与浏览器规格；
   另读 `render-refusals.ts`、`book-journal.ts`、`assistant-journal.ts`、`editor-journal.ts`、
   `file-task-policy.ts` 作对照。
6. 基线（`/private/tmp/sn802`，`origin/main` 内容）：`pnpm test` → **1030 / 64**；
   `pnpm build` + `pnpm test:behavior` → **16 passed**。
7. 新建 detached 副本 `/private/tmp/sn802-run`（`b33bf2cf`）：`pnpm test` → **1043 / 64**；
   `pnpm typecheck` → 5 项目 Done；`pnpm build` + `test:behavior` → **18 passed**；
   `pnpm test:a11y`（behavior 结束后顺序跑）→ **52 passed**。
8. `gh pr checks 801` → 20 pass / 2 skipping / 0 fail。
9. 迁移/回滚探针（副本内临时 `__probe.test.ts` + `__old-command-journal.ts`，从
   `git show origin/main:…` 取旧实现）：P1–P5 输出见下。
10. 变异 M1/M2/M3（每次一条 hunk，跑完 `git checkout` 复原）→ 27 / 3 / 6；
    M1、M3 另各跑一次 `pnpm build` + `playwright -g "刷新之后"` → 两条刷新规格转红，
    红点分别在 `:636`（M1，会话内就先红，因为分类整体丢失）与 `:643`/`:684`（M3，重载之后）。
11. M4（删动作守卫）：浏览器 → `:471` 红、新 `:665` 仍绿；jsdom 面板套件 → **0 failed**；
    探针 → `fireEvent.click` 0 次、`props.onClick()` 1 次。
12. 清理副本内探针与旧实现文件；`/private/tmp/sn802` 的 `git status` 仅剩本报告待提交。

## 实测命令与输出（摘录）

PR head 单测：

```text
 Test Files  64 passed (64)
      Tests  1043 passed (1043)
```

基线（`origin/main` 内容）：

```text
 Test Files  64 passed (64)
      Tests  1030 passed (1030)
```

PR head `pnpm build && pnpm test:behavior`（节选新增两条）：

```text
  ✓  17 tests/web/render-refusal.behavior.spec.mjs:618:1 › 429：刷新之后仍然可以按原键重放，且只重发同一次派发 (2.6s)
  ✓  18 tests/web/render-refusal.behavior.spec.mjs:665:1 › 415：刷新之后仍然不可重放，强制点击也不发出已结算的键 (3.7s)

  18 passed (15.3s)
```

`pnpm test:a11y`：`52 passed (18.2s)`。

迁移/回滚探针（P1–P5）：

```text
P1 旧记录 rejected:true → load() = {version:1, command:{…}, uncertain:false, refused:"unclassified"}
   sessionStorage 字节未变: true
P2 旧记录 → clear() 写出: {"version":1,"command":null,"uncertain":false,"refused":null}
P3 旧记录 → save(load()) 写出: {"version":1,"command":{…},"uncertain":false,"refused":"unclassified"}
P4 旧解析器读新记录:
   [retryable]    threw: Invalid private recovery record | bytes untouched: true
   [terminal]     threw: Invalid private recovery record | bytes untouched: true
   [unclassified] threw: Invalid private recovery record | bytes untouched: true
   [refused:null] threw: Invalid private recovery record | bytes untouched: true
   [empty record] threw: Invalid private recovery record | bytes untouched: true
P5 新解析器读旧记录:
   [rejected:true]  => {"version":1,…,"uncertain":false,"refused":"unclassified"}
   [rejected:false] => {"version":1,…,"uncertain":true,"refused":null}
```

变异 M1（分类不落库）jsdom：

```text
### M1-drop-category: total=103 failed=27 files_failed=4
   RED: command-journal.test.ts :: keeps a first definitive rejection distinct from an unknown outcome
   RED: command-journal.test.ts :: persists the category of a retryable rejection, so a reload reads the same one
   RED: command-journal.test.ts :: keeps the unknown outcome when a classified refusal follows an unobserved attempt
   RED: RenderUnitPanel.test.tsx :: 429 rate limited offers the same handle before and after a reload
   RED: RenderUnitPanel.test.tsx :: cleanup wait offers the same handle before and after a reload
   RED: RenderUnitPanel.test.tsx :: raw cleanup 409 offers the same handle before and after a reload
   RED: RenderUnitPanel.test.tsx :: 415 media unsupported offers the same handle before and after a reload
   RED: RenderUnitPanel.test.tsx :: 403 permission denied offers the same handle before and after a reload
   RED: RenderUnitPanel.test.tsx :: 409 idempotency conflict offers the same handle before and after a reload
   RED: RenderUnitPanel.test.tsx :: keeps the original-key replay after <各码> exactly when its copy promises a retry × 16
   RED: RenderUnitPanel.test.tsx :: reads the raw cleanup 409 as the retryable wait, and keeps its own code for diagnosis
```

变异 M2 / M3：

```text
### M2-legacy-retryable: total=103 failed=3
   RED: command-journal.test.ts :: reads a record written before #798 as an unclassified refusal, deterministically
   RED: command-journal.test.ts :: migrates a restored legacy record to the current shape when it is saved
   RED: RenderUnitPanel.test.tsx :: keeps a record written before #798 unreplayable, and says only what it holds

### M3-restore-unclassified: total=103 failed=6 files_failed=2
   RED: RenderUnitPanel.test.tsx :: 429 rate limited offers the same handle before and after a reload
   RED: RenderUnitPanel.test.tsx :: cleanup wait offers the same handle before and after a reload
   RED: RenderUnitPanel.test.tsx :: raw cleanup 409 offers the same handle before and after a reload
   RED: RenderUnitPanel.test.tsx :: 415 media unsupported offers the same handle before and after a reload
   RED: RenderUnitPanel.test.tsx :: 403 permission denied offers the same handle before and after a reload
   RED: RenderUnitPanel.test.tsx :: 409 idempotency conflict offers the same handle before and after a reload
```

M1 下浏览器刷新规格（红点在会话内，说明分类整体丢失）：

```text
  ✘  1 …render-refusal.behavior.spec.mjs:618:1 › 429：刷新之后仍然可以按原键重放，且只重发同一次派发
       Error: expect(locator).toBeEnabled() failed
       > 636 |   await expect(replay(units)).toBeEnabled();
  ✘  2 …render-refusal.behavior.spec.mjs:665:1 › 415：刷新之后仍然不可重放，强制点击也不发出已结算的键
       > 684 |   await expect(retainedIntent(reloaded)).toContainText("服务端已确定性拒绝这次派发");
           actual: 「…这条记录只说明服务端拒绝过这次派发，没有保存该拒绝是否可重试…」
```

M3 下红点落在**重载之后**（证明规格测的是重挂载状态）：

```text
  ✘  1 …:618 429：刷新之后仍然可以按原键重放…
       > 643 |   await expect(replay(reloaded)).toBeEnabled();
  ✘  2 …:665 415：刷新之后仍然不可重放…
       > 684 |   await expect(retainedIntent(reloaded)).toContainText("服务端已确定性拒绝这次派发");
```

M4（删除动作守卫 `RenderUnitPanel.tsx:469`）：

```text
浏览器： ✓ :665 415：刷新之后仍然不可重放，强制点击也不发出已结算的键   ← 空断言，仍绿
        ✘ :471 415：两层守卫之下，可点的重放控件也发不出已结算的键
          Error: a settled key is not re-sent even when its handler is invoked directly  (> 512)
jsdom： failed = 0 of 90
探针： PROBE1 generate calls after fireEvent.click: 0
      PROBE2 generate calls after direct handler: 1
```

`gh pr checks 801`：20 pass（含 `Web: lint, types, tests, build and accessibility`、
`Gate: required checks for this change`）/ 2 skipping / 0 fail。

文档门禁：

```text
$ uv run --no-project ruff format --check .
… files already formatted
```
