# PR #786 `fix/782-run-notices` 非作者审查（#782）

- 审查对象：PR #786 `fix: #run 的手写 notice 统一到可读身份判据，status 与 alert 不再指向不同对象 (#782)`
- head：`7b91d0330362a209c498b2e8ebe84ba36bc6b70b`；base（merge-base）：`37d06985`（#780/#781 已合入）
- 审查分支：`review/787-run-notices`，由 `origin/main`（`aa125e49`）新建；独立 worktree `/private/tmp/sn787`
- 被审内容读取方式：`git show 7b91d033:<path>`；变异与实跑在一次性 worktree `/private/tmp/sn787-pr`（detached `7b91d033`）与 `/private/tmp/sn787-base`（`37d06985`、`1ac061ef`）完成，未 push 任何变异
- 报告文件名：`docs/reviews/2026-11-14-run-notices.md` 在 `origin/main` 的 47 个既有 review 文件中**不存在**，采用原名，未覆盖任何既有审查记录
- 环境：系统 node `v26.12.1`（mise shim → `/opt/homebrew/bin/node` 26.12.1），pnpm `12.14.0`，vitest `5.0.5`，Playwright `1.64.1`

## 结论

**修复方向正确，可以合入，但需先处理 2 项「必须修」。** `#run` 内 19 条 notice 已真正改为由 `readTargetAction` 一张表生成，`status` 与 `alert` 的读目标在 19 个状态上逐条相等，alert 文案逐字未变；反例 1/3、以及「删掉等式断言仍 1006 全绿」三条自证我都独立复现成立。两处必须修集中在**断言的自证强度**与**「同表」的完整性**：

1. 「恰好 6 对可达组合 / `(operation, canReplay=true)` 与 `(job, canReplay=false)` 不可达」的断言是对**用例表自身**的重言（`cases.map(...)`），不能证明可达性；我用协议合法的错误信封构造出 **`(operation, canReplay=true)` 可达**（`409 idempotency_conflict` + `nexus.operation_id` → `readTarget=operation, canReplay=true`），与代码注释「operation never combines with a replay」直接矛盾。
2. `resumedNoticeFor()` 与 `restore()` 的 5 条状态文案仍**手写读对象**（其中两处是表项的逐字副本、一处是第三种措辞），所以「status 文案统一到一张表（与 alert 同表）」只对 `#run` 成立；恢复路径上 alert 与 status 仍是两份副本。今天逐字相等所以无现网矛盾，但这是本卡要消除的同一类漂移机制。

其余 3 项为「应当修」（恢复态同页覆盖、新硬断言的职责边界需在报告/注释里说清、重复文案），细节见文末**必须修清单**。

## 一、A1：status 与 alert 是否真同表

### 1.1 同表核对（贴代码）

`web/workbench/src/file-workflow/file-task-session.ts`（`7b91d033`）：

```ts
// L311-315：唯一短语表
export const readTargetAction: Readonly<Record<ReadTarget, string>> = {
  operation: "读取原执行核对",
  job: "读取原任务核对",
  asset: "核对原素材",
};

// L324-330：status 的动作子句（#run 的所有 status 都经 #noticeWithActions 走这里）
function actionClauseFor(actions: UnconfirmedActions): string {
  const read = actions.read === null ? null : readTargetAction[actions.read];   // ← 同一行
  if (read && actions.replay) return `请${read}，或按原输入与原键重放`;
  if (read) return `请${read}`;
  if (actions.replay) return "请按原输入与原键重放";
  return "当前页没有可读取的原素材、原任务或原执行";
}

// L339-346：alert
export function unconfirmedAlertFor(actions: UnconfirmedActions): string {
  const read = actions.read === null ? null : readTargetAction[actions.read];   // ← 同一行
  ...
}
```

两个消费者读的是**同一张表的同一个 key**，且喂给它们的 `read` 是**同一个值**：

```ts
// L610-620：#noticeWithActions 用 #readTarget(next)（= readTargetOf(#readableIdentities(next))）
#noticeWithActions(update: Partial<TaskView>, fact: string, tail = "") {
  const next = { ...this.#view, ...update };
  const clause = actionClauseFor({ read: this.#readTarget(next), replay: next.canReplay });
  this.#update({ ...update, notice: `${fact}${clause}。${tail}` });
}
```

`FileTaskPanel.tsx` L108 / L112-113：status 是本快照的 `state.notice`，alert 用同一快照字段：

```tsx
<p role="status">{state.notice}</p>
...
{state.unknown && (
  <p role="alert">{unconfirmedAlertFor({ read: state.readTarget, replay: state.canReplay })}</p>
)}
```

`#update`（L605-611）在写入时用 `#derive` 重算 `readTarget`，`#noticeWithActions` 在写入前用同一组 `#readableIdentities` 预计算，两次调用之间不改任何实例字段（`#record`/`#upload`/`#assetHintId`），所以子句里的 `readTarget` 与落盘后 `state.readTarget` 恒等。**判定：`#run` 的 status 与 alert 确实同表、同输入，不是「各读一个字段后巧合相等」。**

「alert 文案逐字未变」亦成立：`7b91d033` 对 `unconfirmedAlertFor` 只把内联三元换成表查（L340），四条 return 字面量一字未动；逐字钉死 8 种 `(read, replay)` 形状的 `file-task-persistence.test.ts`（L972-999）不在本 PR 改动文件内，PR head 全绿。

### 1.2 仍手写的 status（点名）

`#run` 的 19 条已收敛；**恢复/构造路径没有**：

| 位置（`7b91d033`） | 文案 | 读对象 | 性质 |
| --- | --- | --- | --- |
| `resumedNoticeFor()` L403 | `已找到原执行身份；…请读取原执行核对，不要新建任务。` | operation | 表项逐字副本（手写） |
| `resumedNoticeFor()` L405 | `已找到原任务身份；…请读取原任务核对，或按原输入与原键重放。` | job | 表项逐字副本（手写） |
| `restore()`/构造器 L543 | `已找到原任务身份；该取消请求已被协议确定性拒绝并作废。请读取原任务核对，再显式收尾。` | job | 表项逐字副本（手写） |
| `restore()`/构造器 L548 | `已找到原任务身份；请读取原任务状态。` | job | **第三种措辞**（不是表项） |
| 构造器 L503 / L506 | `已保留原素材标识，请显式读取；…` / `已找到原素材身份。请恢复读取后继续原任务。` | asset | 手写读指令 |

其中 `resumedNoticeFor()` 恰恰是**与 alert 同页渲染**的那条（`record.pending !== null && settled === undefined` → `unknown=true`，alert 同时出现），即本卡「同页两句话」的核心场景在恢复路径上仍是两份副本；今天逐字相等（`file-task-persistence.test.ts` L58-62 逐字钉住），但没有构造上的保证，改表即漂移。`restore()` 的 settled 文案虽不显示 alert（`unknown=false`），但同页另有 `FileTaskPanel` 的 settled `role=status` 段落（已改为表驱动，L117-129），两段 role=status 对同一读给出不同措辞（「读取原任务状态」vs「读取原任务核对」）。

**判定：A1 部分成立。** `#run` 部分（本卡主体）彻底成立；「status 文案统一到一张表」的表述对恢复路径不成立，属必须修或必须显式限定范围。

## 二、A2：19 状态 → 6 对可达组合

19 个用例（`file-task-notices.test.ts` L193-446）逐条核对，映射与语义一致：丢答复 `(asset,true)`、崩溃窗重放再丢 `(asset,true)`、413 非首答 `(asset,true)`、503 无执行身份 `(asset,true)`、503 有执行身份 `(operation,false)`、409 `operation_in_progress` `(operation,false)`、413 首答带身份 `(operation,false)`、503 待决取消 `(job,true)`、409 `job_retry_not_allowed` 待决取消 `(job,true)`、401 `(asset,true)`、429 `(asset,true,unknown=false)`、上传 hint `(asset,false)`、上传无标识 `(null,true)`、hint 未保存 `(null,false)`、重载继续上传被拒 `(null,false)`、输入指纹不符 `(null,false)`、文本回读不可用 `(asset,true)`、日志失败 `(null,true)`、读失败 `(asset,true)`。没有把状态硬塞进不合适对的情况。

但「**不可达组合只有 2 个**」这一说法不成立，且「恰好 6 对」的断言不能证明它：

```ts
// file-task-notices.test.ts L492-503
const pairs = [...new Set(cases.map((row) => `${row.readTarget}:${row.canReplay}`))].sort();
expect(pairs).toEqual([...6 项...]);
```

这是对**手写用例表自身**的重言：它只断言「我写的 19 行里恰好出现 6 种组合」，对产品是否还存在第 7 种组合完全无约束。实测存在两个反例：

**（a）`(operation, canReplay=true)` 可达**（reviewer 探针，协议信封见下）：

```
PROBE-A readTarget= operation canReplay= true unknown= true
PROBE-A notice= 提交被拒绝（409 idempotency_conflict），但该拒绝不能证明原提交未执行；保留原键与未知状态。请读取原执行核对，或按原输入与原键重放。不要新建任务。
PROBE-A alert = 请求结果尚未确认。请读取原执行核对；若重放，只使用原输入和原键。
```

构造：create 请求收到 `409 idempotency_conflict` 且信封带 `nexus.operation_id`。该错误不在 `rejectionTable`、不以 `unknown` 结尾、非 413、非 create 的 `execution_cleanup_pending` → `rejectionDisposition` 判为 `terminal`；`terminal` 分支里 `settlesCreate` 因 `envelopeOperationId` 为 false → `refusalProvesNothing` 成立 → 执行 `{ unknown: true, canReplay: true, recovery: retainRecovery(...) }`，而 `retainRecovery` 的既定职责就是**保留带 `operation_id` 的信封**，于是 `#readTarget` 命中 `operation`、`canReplay` 仍为 `true`。此时文案（表驱动，故 status 与 alert 仍同名）会同时提供「查执行」与「按原键重放」，与注释「An identified execution is queried, not replayed」矛盾。契约只为 `operation_in_progress`/`response_not_replayable` **规定**了该字段，`idempotency_conflict` 只承诺指向一个 Job，因此这是「协议允许但未承诺」的输入；但代码没有任何 guard 把它排除，注释却把它写成不可达。

**（b）`(job, canReplay=false)` 可达（但 `unknown=false`，不在「未确认」作用域内）**：

```
PROBE-B accepted readTarget= job canReplay= false unknown= false
PROBE-B failed-read readTarget= job canReplay= false unknown= false
PROBE-B notice= 读取失败。请读取原任务核对。
```

已接受 Job 后一次读取失败即得 `(job,false)` 并真实渲染。注释把断言限定为 `unknown` 作用域，所以这条不推翻注释本身，但说明「6 对」是**该测试表的形状**，不是产品状态空间；而 `#noticeWithActions` 也被用于 `unknown=false` 的 receipt 类状态（「任务已接收」「素材尚未就绪」「读取失败」），用例表却只有 1 行 `unknown=false`。

**判定：A2 映射逐条成立；穷尽性不成立（断言重言 + 至少一个作用域内反例）。**

## 三、B：断言是否独立

### 3.1 反例 2 独立复现（删等式断言 → 全绿）

在 `/private/tmp/sn787-pr` 删除 `FileTaskHost.test.tsx:568`：

```ts
expect(namedReads(status?.textContent ?? "")).toEqual(namedReads(alert.textContent ?? ""));
```

```
web/workbench test:  Test Files  64 passed (64)
web/workbench test:       Tests  1006 passed (1006)
```

复现成立。等式确实被逐项断言逻辑蕴含：19 行矩阵把 `status` 钉在声明的 `readTarget`（表驱动生成），`file-task-persistence.test.ts` L972-999 把 `unconfirmedAlertFor` 的 8 种形状逐字钉死（同为表驱动），两边同表同输入 ⇒ 等式恒成立。**这是构造保证，不是断言保证。**

### 3.2 新加的两条硬断言是否真独立（两方向变异）

两条新断言：

- **R1**（`file-task-notices.test.ts` L472-478）：每个用例清空 `h.reads` 后调真实 `restore()`，断言实际发出的读种类等于声明的 `readTarget`（`null` 行必须不读）；
- **R2**（`FileTaskHost.test.tsx` L572-574）：真实点击「读取原状态」，断言 `getAsset` 被调用且参数为 `"asset-demo"`（DOM 接线 + id 级 ground truth）。

| 变异 | 内容 | 结果 |
| --- | --- | --- |
| M1 文案点错对象、路由不动 | `operation_in_progress` 分支改回手写「请读取原任务核对」 | **红**（仅文案断言） |
| M2 路由改道、文案不动 | `refresh()` 的 asset 分支改读 `getAsset("asset-other")` | **红**（R1 + R2） |
| M3 文案点错对象 + 关闭文案断言 | M1 + 注释掉 L463-470 的 `namedReads`/`READ_VERB`/`重放` 断言 | **全绿 1006/64** |
| M4 gate 改道 | `restore()` 闸门改回 `if (this.#record || this.#upload)` | **全绿**（不可观测，符合 #780 S1 结论） |

M1 输出：

```
FAIL  src/file-workflow/file-task-notices.test.ts > #run notices name exactly the actions this snapshot enables > 409 operation_in_progress against the identified original execution
AssertionError: expected [ 'job' ] to deeply equal [ 'operation' ]
 ❯ src/file-workflow/file-task-notices.test.ts:464:32
 Tests  1 failed | 21 passed (22)
```

M2 输出（文案断言全过，仅路由断言失败）：

```
FAIL  src/file-workflow/file-task-notices.test.ts > ... > lost create answer: the only read is the confirmed input Asset
AssertionError: expected [] to deeply equal [ 'asset' ]
 ❯ src/file-workflow/file-task-notices.test.ts:476:37
FAIL  src/file-workflow/FileTaskHost.test.tsx > ... > names one read target on both the status line and the alert of the same page
AssertionError: expected 'asset-other' to be 'asset-demo'
 Tests  13 failed | 40 passed (53)
```

M3 输出：

```
web/workbench test:  Test Files  64 passed (64)
web/workbench test:       Tests  1006 passed (1006)
```

**判定：两条新硬断言彼此独立，但它们不守卫「等式」，也不守卫「文案正确性」。**

- R1 与 R2 覆盖不同故障面（session 级 dispatch vs DOM 点击 + 参数），M2 证明二者都会对「路由改道」变红；R2 还额外覆盖「按钮没接线/参数传错 id」。
- 但 M3 证明：把文案改回点错对象、同时关掉文案断言后，两条硬断言与全套件**依然全绿**。所以「等式」不是被它们守住的（B3 已证是构造保证）；这两条断言的职责是「文案承诺的读必须真的发生」，不是「两句话必须同名」。
- R1 的 oracle 仍是同一个 `readTargetOf`（`refresh()` 也用它），所以对谓词本身或闸门的退化不可见（M4 全绿）；真正独立于实现的是用例表中**手写的 `readTarget` 声明**（L456-460）——它才是能抓谓词回归的 oracle。

建议：把 R1/R2 的职责写进测试注释（它们防的是「承诺了却不做」，不是「两句不等」），并把「6 对穷尽」改成对 `actionClauseFor(read, replay)` 全域的穷举或直接删除该表述。

## 四、C：三类未改的定性

1. **`UploadInputMismatchError` → 「请重新提供原文件或原文本」（L727-728）**：定性**成立**。它既不是读（readTarget 在此状态为 `null`）、也不是重放（`canReplay=false`），而是第三种 affordance（重新提供输入），由 `requiresInput`/upload 记录驱动；可达路径 `#resumeInput` 只在 `upload.pending === "upload" && assetHintId === null` 时进入，此时构造器已把 `requiresInput` 置真，因此指令与可用动作一致。**不是被漏掉的第二套判断**，但它是**状态相关**的手写指令却不去查 `requiresInput` 标志：当前唯一可达路径恰好一致，属可接受风险，建议在注释里把它列为「第三类 affordance」以免下次误并表。
2. **终态 `contractError.message`（L1017）**：定性**成立**。`terminal` 分支置 `unknown:false`、无 alert 同页，展示协议原文（含服务端措辞）不是本页的动作判断；`refusalProvesNothing` 之外的非 settle 路径同样只贴协议原文。
3. **`refresh()`/`reconnect()` 结果回执（L1299/1309/1322/1332/1408）**：定性**成立**，是「刚完成的读」的事实陈述，不要求动作。但作者清单**漏了一条同类手写读指令**：`FileTaskPanel.tsx` L85 下载失败时的 `"制品格式与本次原始 JSON 请求不一致，请重新读取任务。"`——这是一句手写「读」指令；该状态必有 Job（`download(id)` 只对已有制品可用），readTarget 恒为 `job`，所以今天不会错，但它不在三/四类清单里，且同样属于「文案自行判断可读对象」。
4. 另注意 `refresh()` 中「已读取原任务；取消请求仍未确认，保留原恢复身份。」（L1309）会与 `unknown=true` 的 alert 同页：status 是回执、alert 才给动作，两者不冲突，但说明「回执类」也可以出现在 alert 同页，不能一概当作无关文案。

## 五、D：越界与副作用

### 5.1 `scripts/dev-env/workbench-tts-host-probe.mjs` 的 2 行同步

diff 只有两处 `getByText(...)` 的字面量：`读取失败，请重新读取。` → `读取失败。请读取原任务核对。`。必要性**成立**：该探针用 `{ exact: true }` 逐字匹配 status 文案，而本卡**就是**改这句话；不改则探针必红。最小性**成立**：只改断言字面量，无逻辑/等待/选择器变更（`git show --stat` 该文件 2 insertions / 2 deletions）。新文案与该状态一致（两处上下文都有已确认 Job → `readTarget=job` → 子句「请读取原任务核对」）。唯一可议之处：探针本可以改成引用 `readTargetAction`（像单测那样），但它是不导入 TS 的 dev 脚本，逐字同步是可接受的取舍。

### 5.2 `check-operation-coverage.py` 的 `tests_drift` 修法

实际改动不在 `tests/unit/test_operation_coverage.py`，而在 `FileTaskHost.test.tsx`：取 status 由 `screen.getByRole("status")` 改为 `document.querySelector(".file-task-detail > p[role=status]")`，并留注释说明原因；**已提交的审计 map 未动**。

我独立复现了 drift 机制（不装 pytest，直接用脚本加载审计模块对比 `differences`）：

```
# PR head（用属性选择器）
differences on PR head: []

# 改回 screen.getByRole("status")
[{"code": "tests_drift", "id": "getWorkbenchStatusV2", "detail": "expected [...15 个文件... 'web/workbench/src/file-workflow/FileTaskHost.test.tsx' ...]"}]
```

根因在扫描器 `test_pattern()`：`patterns.append(rf"[\"']{re.escape(method)}[\"']")`，而 `getWorkbenchStatusV2` 的客户端方法名是 `status`，于是**任何测试里带引号的 `"status"`（例如 `getByRole("status")`）都被当作该 operation 的测试证据**。

判定：
- **修法正确**：往 map 里补 `FileTaskHost.test.tsx` 等于记录一条假证据（该文件根本没调用 `WorkbenchClient.status()`）；改扫描器又超出本卡范围。用属性选择器既让 map 保持真实，也与既有 `tests/web/file-task.behavior.spec.mjs:135` 的 `notice()` 写法一致。**副作用**：新测试因此不使用 Testing Library 的 role 查询，语义断言略弱（只验 `role` 属性，不验 role 计算），这是被扫描器逼出来的测试质量让步，值得在扫描器修好后回改。
- **「扫描器把任意含操作名的字符串当测试证据」应当单独开卡**，证据不止本次：已提交 map 里 `getWorkbenchStatusV2` 的 15 个测试文件中，`FixedTranscriptDownload.test.tsx` 与 `InputDevicePicker.test.tsx` 全文只有 `getByRole("status")`，与该 operation 无关——**既有审计记录已含假证据**。这类泛化方法名（`status`/`job`/`asset`）会把无关测试不断吸进 map，逼着所有人绕开 role 查询。建议单独开卡：quoted-method 启发式加「同一文件必须出现客户端方法调用/`validate(...)`/路径」约束，或改为按 import + 调用点解析。

## 六、E：反例与数字

### 6.1 反例 1（operation_in_progress 改回手写）—— 复现成立

```
AssertionError: expected [ 'job' ] to deeply equal [ 'operation' ]
 ❯ src/file-workflow/file-task-notices.test.ts:464:32
 Tests  1 failed | 21 passed (22)
```

### 6.2 反例 3（413 非首答改回手写「请读取原任务」）—— 复现成立

```
AssertionError: expected [] to deeply equal [ 'asset' ]
 ❯ src/file-workflow/file-task-notices.test.ts:464:32
 Tests  1 failed | 21 passed (22)
```

### 6.3 例数核对

| 对象 | 实测 |
| --- | --- |
| PR base `37d06985`（merge-base）| workbench **63 files / 983 tests**（admin 19/151）|
| PR base 的父提交 `1ac061ef` | workbench 63 files / **933** tests |
| PR head `7b91d033` | workbench **64 files / 1006 tests**（admin 19/151）|
| `pnpm test:behavior`（build 后）| **9 passed** (3.8s) |
| `pnpm test:a11y` | **52 passed** (18.2s) |
| `pnpm typecheck` / `pnpm build` | 全绿 |

**「基线 63/983」是对的**（= merge-base `37d06985`），增量 +1 file / +23 tests = 新文件 22 条（19 用例 + 1 穷尽性 + 2 非未确认状态）+ host 测试 1 条。**卡上的 973 与任何相邻提交都不符**（`1ac061ef` 是 933，`37d06985` 是 983），属过期/错记数字，以作者口径为准。

`gh pr checks 786`（head 与 `7b91d033` 一致，`MERGEABLE`）：Python 3 项、Web 1 项、Integration 0-5、Gate 4 项、Compose/Deploy/Docs/Scope/Node-Controller 全部 **pass**；2 项 `skipping`（`Docs: markdown code block formatting`、`Node Controller image evidence / Build and probe`，路径过滤的预期跳过）。**CI 全绿成立。**

### 6.4 补充独立核验：修复前「几个状态不等」

我用 PR 同一套 harness 在 base worktree（`37d06985`，旧代码）跑同一 19 个状态，用读动词正则统计旧 status 点名的对象：

| # | 用例 | readTarget | 旧 status 点名 | 判定 |
| --- | --- | --- | --- | --- |
| 1 | 丢 create 答复 | asset | job | 不等 |
| 2 | 崩溃窗重放再丢 | asset | job | 不等 |
| 3 | 413 非首答 | asset | 无 | 不等（漏读） |
| 4 | 503 无执行身份 | asset | operation | 不等 |
| 5 | 503 有执行身份 | operation | operation | 等 |
| 6 | 409 operation_in_progress | operation | operation | 等 |
| 7 | 413 首答带身份 | operation | operation | 等 |
| 8 | 503 待决取消 | job | operation | 不等 |
| 9 | 409 job_retry_not_allowed | job | job | 等 |
| 10 | 401 | asset | 无 | 不等（漏读） |
| 11 | 429（unknown=false，无 alert）| asset | 无 | 不计（无同页 alert）|
| 12 | 上传 hint | asset | asset | 等 |
| 13-16 | 无标识/指纹不符等 | null | 无 | 等 |
| 17 | 文本回读不可用 | asset | 无 | 不等（漏读） |
| 18 | 日志失败 | null | asset | 不计（readTarget 为 null）|
| 19 | 读失败 | asset | 无 | 不等（漏读） |

按「有 alert 且 `readTarget` 非 null 且 status 未点名该对象」计 = **8 行（1,2,3,4,8,10,17,19）**，与作者自报的 8 一致；把 null 行（18）和漏读行全部计入则为 10。修复后同表复算 19 行全部相等。

**对卡片背景的两处勘误**（不影响 PR 结论）：旧代码里 **413 非首答的 status 并没有说「请读取原任务」**，而是完全不点名读对象（「…保留原键与未知状态，不要新建任务。」）；**409 `job_retry_not_allowed` 的待决取消旧文案是「请读取原任务」且 readTarget 就是 job，本来是对的**——真正点错对象的是 503 待决取消那条（旧文案「请读取原执行」）。作者 counterexample 3 用「请读取原任务」是**人工改成**的手写文案，不是旧代码原文。

## 必须修清单

### M1（必须）「6 对可达组合」断言不能自证，且 `(operation, canReplay=true)` 与注释「不可达」相反

- 文件：`web/workbench/src/file-workflow/file-task-notices.test.ts` L484-503；`file-task-session.ts` L975-1000（`refusalProvesNothing` 分支）、L953-957 注释。
- 问题：断言对 `cases` 自身取集合，不能证明可达性；实测 `409 idempotency_conflict` + `nexus.operation_id` 得到 `(operation, canReplay=true)`，与「An identified execution is queried, not replayed」「operation never combines with a replay」冲突。
- 建议（二选一）：① 产品侧在 `refusalProvesNothing` 分支把 `canReplay` 收紧为 `!retainRecovery(...).error.nexus?.operation_id`（与 `unknown-outcome` 分支同规则），并补一条 `(operation,false)` 用例；② 若认定该信封不合契约，则**删掉/限定**「恰好 6 对可达」「只有 2 个不可达」的表述，并把注释改成「本表覆盖的 6 种形状」，避免下一位审查者把重言当证明。

### M2（必须）「status 统一到一张表」对恢复路径不成立，需收敛或限定

- 文件：`file-task-session.ts` `resumedNoticeFor()` L401-407、`restore()`/构造器 L543/L548（以及 L503/L506）。
- 问题：`resumedNoticeFor()` 与 alert 同页，却仍是表项的手写副本；L548 是第三种措辞（「请读取原任务状态」）。改表即漂移，只靠 `file-task-persistence.test.ts` 的逐字断言事后发现。
- 建议：至少让 `resumedNoticeFor()` 与 `settledCancel` 那两句改用 `readTargetAction`/`actionClauseFor` 生成（保留各自 fact 前缀与 tail）；如本卡不扩范围，则把 `readTargetAction` 的文档注释从「the one table every sentence …」改为「`#run`/`#submit` 的 status 与 alert」，并在报告/注释中登记恢复路径为已知未收敛项。

### M3（应当）同页等式测试只覆盖 `#run` 路径，未覆盖被点名的动机状态

- 文件：`FileTaskHost.test.tsx` L550-575。
- 问题：新测试选的是「崩溃窗 + 重放再丢答复」（status 走 `#run`，已同表）；而**纯恢复的崩溃窗**（`resumedNoticeFor` 路径）同页仍是 status 不点名、alert 点 `asset`（既有测试 L523-548 正钉住这一不对称）。本卡以此为动机，却把该状态排除在新等式断言之外。
- 建议：把该测试扩到纯恢复崩溃窗，或在注释中显式登记「恢复态由 #771 刻意保留，不在本卡等式范围」，否则「一页不再说两个对象」的表述过宽。

### M4（应当）两条硬断言的职责需落到注释/文档，避免被误读为「等式的守卫」

- 文件：`file-task-notices.test.ts` L472-478、`FileTaskHost.test.tsx` L572-574。
- 依据：M3 变异证明关掉文案断言后手写错文案全套件仍 1006 绿。建议在两条断言上方注明：它们防的是「承诺了却不做」（dispatch/接线/id），等式由同表生成保证，文案正确性由 `namedReads`/`READ_VERB` 保证。

### M5（小）文案/断言冗余与隐含不变量

- `FileTaskPanel.settledReadAction` 的 `null` 文案（`本页没有可读取的原素材、原任务或原执行`）与 `actionClauseFor` 的 `null` 文案（`当前页没有可读取的原素材、原任务或原执行`）是两句近似重复，建议共用一处常量。
- `file-task-notices.test.ts` L476-478 中 `built.reads` 对 `null` 行恒为 `[]`，`row.readTarget === null ? built.reads : [row.readTarget]` 等价于 `[]`，可简化以免读者以为它携带额外 ground truth。
- `#noticeWithActions` 依赖「`next` 计算与 `#update` 之间不改实例字段」的隐含不变量，建议在注释里点明，降低后续插入 `#persist`/`#acceptJob` 时的静默漂移风险。

## 只能依赖作者陈述的点

- 「修复前有 8 个状态两者不等」的**计数口径**是作者的；我用「有 alert 且 readTarget 非 null 且 status 未点名」重建出恰好 8 行（1,2,3,4,8,10,17,19），其余口径（含漏读/null 行）为 10 行，无法唯一反推作者口径。
- 「卡上允许改动的文件清单」与实际越界的边界由卡片给出；我只判断了 `workbench-tts-host-probe.mjs` 的 2 行同步在**技术上**必要且最小，无法确认它是否在卡片白名单内。
- `UploadInputMismatchError`「动作是重新提供输入、属第三类 affordance」的**分类**是作者的；我核对了该分类与代码可达性一致，但「第三类」这个词的边界定义来自作者。
- 新增断言是否「足以证明等式逻辑蕴含」取决于 `file-task-persistence.test.ts`（未改动文件）对 alert 8 种形状的逐字断言仍然有效——我按 PR head 实跑确认它通过，但若未来该文件被放宽，B3 的蕴含关系会失效。

## 附录：实跑命令摘要

```
# 独立 worktree（基于 origin/main）
git fetch && git worktree add /private/tmp/sn787 -b review/787-run-notices origin/main
git diff origin/main...HEAD --stat        # 仅本报告文件

# 被审内容在 /private/tmp/sn787-pr（detached 7b91d033）
cd /private/tmp/sn787-pr && pnpm install --frozen-lockfile
pnpm typecheck                            # 全绿
pnpm build && pnpm test:behavior           # 9 passed
pnpm test:a11y                             # 52 passed（与 behavior 串行）
pnpm test                                  # workbench 64 files / 1006 tests；admin 19/151

# 基线（/private/tmp/sn787-base）
37d06985  pnpm test                        # workbench 63 / 983
1ac061ef  pnpm test                        # workbench 63 / 933

# CI
gh pr checks 786                           # 全 pass，2 项路径过滤 skipping

# 变异（仅 /private/tmp/sn787-pr，未 commit/push）
M1 409 operation_in_progress 改回手写       → 1 failed (line 464)
M2 refresh() asset 分支改读 asset-other     → 13 failed (line 476 + host getAsset)
M3 M1 + 注释 L463-470 文案断言              → 1006 全绿
M4 restore() gate 改回 #record || #upload   → 全绿
# 探针（临时文件，已删）
PROBE-A 409 idempotency_conflict + op_id    → (operation, canReplay=true)
PROBE-B accepted Job + 读取失败              → (job, canReplay=false)
# 审计扫描器
python3 - <<'EOF' ... differences(DEFAULT_MAP, derive(ROOT)) EOF
PR head → []；改回 getByRole("status") → tests_drift @ getWorkbenchStatusV2
```

## 时间线

| 时间 | 动作 |
| --- | --- |
| 23:26 | 确认 `docs/reviews/2026-11-14-run-notices.md` 不存在；`git fetch`；从 `origin/main`(`aa125e49`) 建 `/private/tmp/sn787` |
| 23:27 | 读 PR diff（7 文件）、`readTargetAction`/`actionClauseFor`/`unconfirmedAlertFor`/`#noticeWithActions`/`restore()` |
| 23:30 | `/private/tmp/sn787-pr` detached `7b91d033` + `pnpm install`；`/private/tmp/sn787-base` `37d06985` |
| 23:32 | `pnpm test` PR head 1006/64；base 983/63 |
| 23:33 | 复现反例 2（删等式断言 → 1006 全绿） |
| 23:34 | 复现反例 1（`['job']` vs `['operation']`）、反例 3（`[]` vs `['asset']`） |
| 23:35 | 变异 M2（路由改道）→ R1+R2 红；M4（gate）→ 绿 |
| 23:36 | 探针 PROBE-A/B：`(operation,true)`、`(job,false)` 可达；M3：关闭文案断言后错文案全绿 |
| 23:38 | `pnpm typecheck` 绿；`pnpm build` + `test:behavior` 9 绿；`test:a11y` 52 绿 |
| 23:40 | `gh pr checks 786` 全绿；`1ac061ef` 933 计数；审计扫描器 drift 复现 |
| 23:42 | 旧代码 19 状态点名统计（8/10 行不等），完成 A1/A2/C/D 判定 |
| 23:45 | 写本报告，`ruff format --check`，commit/push 并开审查 PR |
