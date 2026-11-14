---
title: PR #789 非作者审查：覆盖证据必须指向真实调用面（#788）
status: active
---

# PR #789（`fix/788-coverage-evidence`）非作者审查记录

被审对象：PR #789《fix: 覆盖证据必须指向真实调用面，收紧后逐条复核实证据 (#788)》，
head `bea0b674b58ed84714988b33fe3801b6d6ec1b8f`，单 commit，base `origin/main`
（`2a3299baa159f8908e8d18d4b7773223b281d96a`），diff 4 文件 `+67 / −23`：
`scripts/dev-env/check-operation-coverage.py`（`test_pattern()` 收紧）、
`tests/unit/test_operation_coverage.py`（+3 回归）、
`docs/audits/2026-10-26-team-c-operation-coverage.json`（147 → 138 条证据）、
`web/workbench/src/file-workflow/FileTaskHost.test.tsx`（3 处选择器回改）。

审查方式：本仓库 `docs/reviews/` 在 `origin/main` 上不存在，本报告为首个文件，
未覆盖任何既有审查记录。独立 worktree 分两处使用——`/private/tmp/sn790` **基于
`origin/main` 拉分支** `review/790-coverage-evidence`（只放本报告，写报告前
`git diff origin/main...HEAD --stat` 为空），`/private/tmp/sn790-pr` 是**同一仓库的
detached worktree，签出于被审 head `bea0b674`**，所有实跑（pytest、vitest、变异、
旧扫描器对照）都在这棵被审树上做；被审代码内容另用 `git show bea0b674:<path>` 与
`git diff origin/main...bea0b674` 交叉读取。变异一律临时施加、单点运行、
`git checkout --` 还原（每轮收尾 `git status --porcelain` 已确认为 0 个 tracked 修改），
**未 push 任何变异**。环境：macOS 宿主，node `v26.12.1`（mise）、pnpm `12.14.0`、
Python 3.14（`uv sync --locked --all-packages --all-groups` 于被审树内自建 `.venv`）。

## 结论

**可以合并：清单 A–D 八项全部成立，无必须修项。**

作者自报的每一个可核数字我都独立复现到，**没有发现夸大或错报**：
证据 147 → 138、删 9 条（5 个 operation）、`42 bound / 10 pending` 不变、
0 个 operation 状态翻转、38 条 quoted-only 证据确为对象绑定 spy、
12 条 fixture-key-only、旧扫描器在修好的树上仍报 5 个 `tests_drift`、
3 个新回归各能单独变红、`getByRole("status")` 回改 3 处、
`test_operation_coverage.py` 18 passed、`tests/unit/backend` 3158 passed、
`tests/contract` 3799 passed、workbench vitest 64 files / 1008 passed，
CI `gh pr checks 789` 全绿（20 pass / 2 skip / 0 fail）。

### 本卡两个核心问题的答案

**「新规则会不会引入新的假阳性」：不会。** 第 3 条 `\.<method>\(` 与旧规则**逐字相同**
（本 PR 只是把它抽成 `escaped_method` 变量，语义未变），第 1 条 `\b<operationId>\b`
同样未变，第 4 条 spy 规则是旧裸引号规则的**严格收窄**（新增对象绑定与字面 `spyOn(`）。
实测 42 个带客户端方法的 operation、78 处 `\.<method>\(` 命中行（同一行命中两个
operation 计两次），逐类判定后**只有一处假阳性类**：`editor-session.test.ts` 里 5 处
`...view("2", …)` 展开运算符命中了 `getTranscript` 的 `view` 方法——而这是**旧规则
本来就有的**（旧 `patterns` 同样含 `\.{method}\(`），不是本 PR 引入。短名方法我逐个
验证：`\.status\(` 全库测试只有 1 行且是真调用（`workbench-client.test.ts:39`）、
`\.book\(` 1 行真调用、`\.project\(` 在测试里**零命中**（只出现在产品代码里）、
`\.projects\(`/`\.books\(`/`\.chapters\(`/`\.voices\(`/`\.deletion\(`/`\.revise\(`
的测试命中也全是真调用。因此「用一个宽松规则换另一个宽松规则」的指责不成立：
本 PR 是单向收窄，且残留的宽松点全部是继承来的。

**「spy 目标算不算行为证据」：在这个代码库里算，但比作者写的理由弱一档。**
38 条 spy-only 证据的 spy 目标对象都是**真的 `WorkbenchClient` 实例**
（`new WorkbenchClient(vi.fn())`），`vi.spyOn` 在运行时对缺失成员会抛错、TS 也会拒绝
写错的成员名，所以对象绑定是**真实结构声明**，不是字符串。但「spy 存在」只证明替身装了，
不证明调用发生过；作者在 PR 里写的理由（「spyOn 会校验成员存在」）只到此为止。
我补了调用侧的验证：38 条里 10 条带同文件 `expect(<spy>).toHaveBeenCalled*` 断言；
**唯一依赖 spy 才能算 bound 的 6 个 operation**（`generateRenderUnitV2`、`getAssistantTurn`、
`getRenderUnitV2`、`getTeleprompterState`、`listRenderUnitGenerationsV2`、
`updateTeleprompterState`）**全部**有显式 `toHaveBeenCalled*` 断言。另做了一次
埋点实验：把 `assistant-session.test.ts` 的 `status` spy 改成抛错后**该文件 7/7 全红**，
说明未断言的 spy 也在真实调用路径上。所以保留这一条**有据**；残留风险是规则本身
仍允许「装了不调用、也不断言」的装饰性 spy（当前树上 0 个 operation 依赖这种情况），
见建议 S3。

## 逐条判定

| # | 检查项 | 判定 | 关键证据 |
| --- | --- | --- | --- |
| A1 | 新规则引入新假阳性 | ✅ 无新增 | 第 3 条与旧规则逐字相同；78 处命中逐类核对，唯一假阳性类是旧的 `...view(` |
| A2 | `\b<operationId>\b` 的 12 条 fixture-key-only 与 0 翻转 | ✅ 数字正确 | 精确复现 12 条；扩到全部 18 条 quoted-id-only 后仍是 0 翻转 |
| A3 | spyOn 保留的定性 | ✅ 定性基本成立（理由需补强） | 38 条 spy-only；6 个 100%-spy-only operation 全有 `toHaveBeenCalled*`；埋点实验 7/7 红 |
| B4 | 9 条删除是否删对 | ✅ 9/9 删对 | 新规则四形态零命中；旧命中全是 ARIA role / journal kind / 拒绝类别 / 类型索引 |
| C5 | 旧扫描器 5 个 `tests_drift` 复现 | ✅ 精确复现 | 固定树上旧扫描器 `findings: 5`、exit 1；反例探针文件只旧扫描器报 |
| C6 | 3 个新回归能否变红 | ✅ 3/3 可红 | M1 3 red、M2 3 red、M3 2 red（单点各 1 hunk） |
| D7 | 审计 map 数字与门禁 | ✅ 一致 | 147 → 138、9 条删与作者表逐条相同、42/10 不变、check exit 0 |
| D8 | `#786` 三处回改与全绿 | ✅ | 556/575/643 三处 `screen.getByRole("status")`，无 `p[role=status]` 残留；1008 passed |

### A1 新规则会不会引入新的假阳性

规则形态（`git show bea0b674:scripts/dev-env/check-operation-coverage.py`）：

```python
patterns = [rf"\b{escaped_id}\b", requested]  # 1 契约 id / 2 METHOD+路径
if method:
    patterns.append(rf"\.{escaped_method}\(")  # 3 客户端方法调用
    patterns.append(rf"\bspyOn\(\s*[^,()\n]+,\s*[\"']{escaped_method}[\"']\s*\)")  # 4 对象绑定 spy
```

第 1、3 条相对 `origin/main` **语义未变**（第 3 条仅把 `re.escape(method)` 提成
`escaped_method`），第 4 条把旧的 `["']<method>["']` 收窄成必须 `spyOn(对象, "方法")`。
因此「新的假阳性」只可能来自第 4 条——而它比旧规则**更严**，不会多匹配。

实测：52 个 operation 中 42 个有客户端方法（`client_methods()` 映射），
65 个测试候选文件；对每个 `\.<method>\(` 逐行取命中并分类，共 78 行：

| 类 | 行数 | 判定 |
| --- | --- | --- |
| `new WorkbenchClient(...).<method>(` / `client.<method>(` 真调用 | 63 | 真证据 |
| `f.session.<method>(`、`c.<method>(` 等 session 包装的真调用 | 10 | 真证据（session 委托到 client 方法，产品链路上确会到达） |
| `...view(` 展开运算符（`editor-session.test.ts` 5 行） | 5 | **假阳性，但旧规则同样如此** |

短名方法逐个核实（卡面点名的可疑类）：

| 方法名 | 命中行 | 结论 |
| --- | --- | --- |
| `status`（`getWorkbenchStatusV2`） | 1（`workbench-client.test.ts:39`） | 真调用；测试里**没有**任何 `x.status(` 形式撞名 |
| `book`（`getBook`） | 1（`workbench-client.test.ts:302`） | 真调用 |
| `view`（`getTranscript`） | 7，其中 5 是 `...view(` | 2 真 + 5 假阳性（旧规则遗产） |
| `revise`（`createTranscriptRevision`） | 1（`workbench-client.test.ts:168`） | 真调用 |
| `project`（`getProject`） | **0** | 测试里无 `\.project\(`；不存在撞名 |
| `projects`/`books`/`chapters`/`voices`/`deletion` | 3/2/2/3/2 | 全为真调用 |

`.revision(` 没有出现：该 operation 的方法名是 `voiceRevision`，不是 `revision`。

另核对第 4 条的两个潜在回归方向：

- **漏判（假阴性）**：`spyOn(` 后要求对象表达式不含换行（`[^,()\n]`）。全库 40 处
  `vi\n  .spyOn(` 断行写法，但对象与方法名都在同一行，规则仍命中；若将来写成
  `vi.spyOn(\n  client,\n  "status"\n)` 会漏。当前树上 0 例，属潜在风险而非现存缺陷。
- **对象不是 WorkbenchClient**：`vi.spyOn(view.client, "createRenderUnit")` 这类
  session 包装上的 client 也是真的 `WorkbenchClient` 实例，无「随便一个对象」的情况。

### A2 `\b<operationId>\b` 仍计的夹具键与 0 翻转

把 id 规则临时改成只认**未被引号包裹**的 id
（`(?<!["'])\b<id>\b(?!["'])`，临时副本 `scripts/dev-env/__idq_probe.py` + `--write`
到临时 map，做完删除并 `git status` 确认无残留），结果：

```
commit   total/bound/pending = (52, 42, 10)   test entries 138
probe    total/bound/pending = (52, 42, 10)   test entries 120   （删 18）
STATUS FLIPS: NONE
```

18 条里恰好 **12 条是 `example("<id>")` / `sample("<id>")` 夹具键**（与作者口径完全一致：
`createProject` 6、`getTranscript` 2、`getWorkbenchStatusV2`/`listProjects`/`listRenderUnits`/
`listTranscripts` 各 1），另 6 条是 V1 operationId 撞上 V2 方法名（`"createRenderUnit"`
`"generateRenderUnit"` `"updateRenderUnit"` 三条 spy 目标）与遥测断言字符串
（`e.operation === "getBookExport"|"getSavedAsset"|"getTranscript"`）。
也就是说 `\b<id>\b` 确实仍偏宽，但**删掉这 18 条后没有一个 operation 掉出 bound**：
12 条夹具键之外每条 operation 都另有真证据，6 条撞名指向的 V1 operation
本身就是 `consumer_pending`（`consumers = 0`，与 tests 无关）。作者「0 翻转」正确。
测试语料里**没有**只出现在注释里的 id（我按 `#`、`//`、`*` 行首分类，命中 0 条）。

### A3 spy 目标算不算行为证据

38 条 spy-only 证据（除 spy 外无 id/路径/方法调用形态）——与作者「38 条 quoted-only
证据保留」的数字**完全一致**。逐条核对：

- **对象是真的客户端**：全部为 `vi.spyOn(<WorkbenchClient 实例>, "<method>")`；
  少数为 `vi.spyOn(view.client, …)`/`vi.spyOn(empty, "renderUnits")`，其值同样是
  `new WorkbenchClient(vi.fn())`。错误成员名会被 TS 拒绝、缺失成员会被 Vitest 抛错，
  所以对象绑定不是装饰。
- **调用侧**：10/38 有同文件 `expect(<spy>).toHaveBeenCalled*` 断言，其余靠
  「mock 返回值喂给被测行为」隐式成立。
- **关键 6 个 operation 的 tests 列表 100% 是 spy-only**，我逐个查了断言，全部有：

| operation | spy-only 证据文件 | 断言 |
| --- | --- | --- |
| `generateRenderUnitV2` | `books/RenderUnitPanel.test.tsx` | `expect(generate).toHaveBeenCalledTimes(1)`（588、648） |
| `getAssistantTurn` | `assistant/assistant-session.test.ts` | `expect(read).toHaveBeenCalledTimes(2)`（117） |
| `getRenderUnitV2` | `books/RenderUnitPanel.test.tsx` | `expect(read).toHaveBeenCalledWith("unit_demo")`（675、700） |
| `getTeleprompterState` | `teleprompter/teleprompter-state.test.ts` | `expect(read).toHaveBeenCalledWith(PROJECT, undefined)`（68） |
| `listRenderUnitGenerationsV2` | `books/RenderUnitPanel.test.tsx` | `expect(history).toHaveBeenCalledWith("unit_demo", 100)`（741） |
| `updateTeleprompterState` | `teleprompter/teleprompter-state.test.ts` | `expect(write).toHaveBeenCalledWith(…)`（89） |

- **埋点实验（未断言的 spy 是否真被调用）**：把 `assistant-session.test.ts` 的
  `statusRead` spy 后追加 `mockImplementation(() => { throw new Error("SPY_TRIGGERED_status") })`，
  跑该文件 → **7/7 全红**（`expected 'unknown' to be 'conflict'` 等），说明 `client.status()`
  在每条用例路径上都被真实调用；跑完 `git checkout --` 还原。

结论：**带对象绑定的 spy 目标在这个代码库里是真实行为证据（比裸字符串强一个量级）**，
但作者给的理由只覆盖「成员存在」，完整理由应是「成员存在 + 这些用例断言或依赖该调用」。
规则仍允许「只装 spy、不调用、不断言」的情形，当前树上没有 operation 依赖它。

### B4 9 条删除逐条核对

对 9 条 (operation, 文件) 逐条用新规则四形态扫描，**全部零命中**（确为假证据而非误删）；
旧规则命中行逐条给出：

| operation | 被删文件 | 新规则 | 旧命中的"证据" | 判定 |
| --- | --- | --- | --- | --- |
| `getWorkbenchStatusV2` | `books/ContentDownload.test.tsx` | 0 命中 | `queryByRole("status")` ×3（断言"没有通知"） | 假证据 |
| `getWorkbenchStatusV2` | `books/RenderUnitPanel.test.tsx` | 0 命中 | `findByRole("status", {name: "音色修订信息"/"单元 1 副本信息"})` | 假证据（aria-label 的 status 区域，非 payload `status:`） |
| `getWorkbenchStatusV2` | `manuscripts/FixedTranscriptDownload.test.tsx` | 0 命中 | `getByRole("status").toHaveTextContent("下载未开始")` | 假证据（卡面点名） |
| `getWorkbenchStatusV2` | `realtime/InputDevicePicker.test.tsx` | 0 命中 | `getByRole("status")` ×3 | 假证据（卡面点名） |
| `getWorkbenchStatusV2` | `teleprompter/TeleprompterPanel.test.tsx` | 0 命中 | `getByRole("status", {name: "本地提词状态"/"部署提词快照"/…})` | 假证据 |
| `createTranscriptRevision` | `manuscripts/editor-journal.test.ts` | 0 命中 | `kind: "revise"`（日志意图种类） | 假证据 |
| `getProject` | `meetings/meeting-journal.test.ts` | 0 命中 | `it.each(["project", "source", …])` 与 `if (kind === "project") r.project!.id …` | 假证据（拒绝用例类别/记录字段） |
| `getVoiceRevisionV2` | `file-workflow/tts-task.test.ts` | 0 命中 | `it.each(["voiceRevision", "profileRevision", "assetId"])` | 假证据（字段名） |
| `listProjects` | `file-workflow/TtsTaskHost.test.tsx` | 0 命中 | `projects: vi.fn<WorkbenchSaveClient["projects"]>()` | 假证据（**类型索引 + fake save client 成员**，不是 workbench 客户端调用/路径/spy） |

卡面特别点名的两处：

- **`RenderUnitPanel.test.tsx` 的 `status:`**：该文件被旧规则计入的命中不是 payload 的
  `status:` 字段，而是 4 处 `findByRole("status", { name: … })`。两种都属于「同名无关字符串」，
  删除正确。
- **`TtsTaskHost.test.tsx` 的 `WorkbenchSaveClient["projects"]`**：类型索引是**编译期类型查询**，
  不产生任何运行期调用；该文件是 TTS 保存流程的测试，测的是 `saveProjectAsset` 与保存 fake，
  与 `GET /v1/workbench/projects` 无关。删除正确，且 `listProjects` 仍有 10 条真证据
  （`AssistantHost`/`BooksHost`/`DeletionHost`/`deletion-session`/`ManuscriptHost`/`editor-session`/
  `workbench-client`/`MeetingHost`/`MeetingNavigation`/`meeting-session`），不会掩盖真实覆盖缺口。
- 其他 8 条同理：每个受影响 operation 删后仍有 2–10 条真证据，无「唯一证据被删」的情况
  （`getVoiceRevisionV2` 剩 2、`createTranscriptRevision` 剩 3、`getProject` 剩 6）。

### C5 旧扫描器的 5 个 `tests_drift` 复现

在**修好的树**（`bea0b674`，`FileTaskHost.test.tsx` 已是 `getByRole("status")`）上，
把 `git show origin/main:scripts/dev-env/check-operation-coverage.py` 落成临时
`scripts/dev-env/__old_scan.py`（`ROOT = parents[2]` 仍指向被审树）运行：

```
{"operations": 52, "bound": 42, "pending": 10, "findings": [ ... 5 条 tests_drift ... ]}
exit=1
```

5 条 id 与作者所述完全一致：`createTranscriptRevision`、`getProject`、`getVoiceRevisionV2`、
`getWorkbenchStatusV2`（其 expected 里含新增的 `FileTaskHost.test.tsx`）、`listProjects`。

反向复现（作者 PR 描述 §2 的探针）：新建只有一行 `expect(screen.getByRole("status"))…`
的临时测试文件 `web/workbench/src/realtime/__status_role_evidence_probe.test.tsx`：

```
旧扫描器: findings 5，getWorkbenchStatusV2 的 expected 里出现 probe 文件 → exit 1
新扫描器: {"operations": 52, "bound": 42, "pending": 10, "findings": []} → exit 0
```

同一操作对 card 点名文件逐个比对新旧 `test_pattern()`：

```
file                                                        old   new
manuscripts/FixedTranscriptDownload.test.tsx                True  False
realtime/InputDevicePicker.test.tsx                         True  False
books/ContentDownload.test.tsx                              True  False
books/RenderUnitPanel.test.tsx                              True  False
teleprompter/TeleprompterPanel.test.tsx                     True  False
file-workflow/FileTaskHost.test.tsx                         True  False
assistant/assistant-session.test.ts   (真证据)              True  True
manuscripts/workbench-client.test.ts  (真证据)              True  True
```

探针文件跑完已删除（`git status` 确认）。

### C6 3 个新回归的变异（单点，各 1 hunk）

每轮只改 `test_pattern()` 一处、单独跑 `uv run pytest tests/unit/test_operation_coverage.py -q`、
跑完 `git checkout --` 还原：

| 变异 | hunk | 期望咬住的断言 | 实测失败 |
| --- | --- | --- | --- |
| M1 加回裸引号 `["']<method>["']` | 1 | `test_role_string_sharing_the_client_method_word_is_not_evidence` | 3 failed：该条 + `test_unbound_quoted_property_does_not_bind_the_method` + `test_committed_map_is_current` |
| M2 删掉对象绑定 spy 分支 | 1 | `test_client_method_spy_target_is_evidence` | 3 failed：该条 + 既有 `test_spy_target_method_name_counts_as_behavior_test` + `test_committed_map_is_current` |
| M3 追加成员索引形式 `\[["']<method>["']\]` | 1 | `test_unbound_quoted_property_does_not_bind_the_method` | 2 failed：该条 + `test_committed_map_is_current` |

3 条新断言都能变红；M3 还给第 3 条一个 M1 之外的**独属守卫**（M1 会让第 1、3 条一起红，
M3 只让第 3 条红）。顺带说明 `test_committed_map_is_current` 是个有效端到端守卫：
任何收放规则而不重生成 map 的改动都会红。

### D7 审计 map 的 diff 与门禁

用 `git show origin/main:docs/audits/2026-10-26-team-c-operation-coverage.json` 与 PR 版逐字段比：

```
origin/main map: total=52 bound=42 pending=10 test-entries=147 consumers=58
PR map         : total=52 bound=42 pending=10 test-entries=138 consumers=58
removed 9 entries / added 0 / status changes: NONE / consumers changed: NONE
pending registrations changed: False / contract_sha256 unchanged: True
audited_head: 99fd0410 -> 2a3299ba
```

删的 9 条与作者表格**逐条相同**（`getWorkbenchStatusV2` 5 条、`createTranscriptRevision` /
`getProject` / `getVoiceRevisionV2` / `listProjects` 各 1 条），`getWorkbenchStatusV2` 由 15 → 10。
门禁：`python3 scripts/dev-env/check-operation-coverage.py` →
`{"operations": 52, "bound": 42, "pending": 10, "findings": []}`，`exit=0`。

一处**措辞/实现差异**（记在建议 S1，不是缺陷）：脚本没有 `--check` 这个 flag
（`argparse` 只有 `--map` / `--write`；传 `--check` 会 `exit 2`
`error: unrecognized arguments: --check`），"检查模式"就是**不带 `--write` 的默认运行**。

### D8 `#786` 三处回改与全绿

`web/workbench/src/file-workflow/FileTaskHost.test.tsx` 3 个 hunk 均把
`document.querySelector(".file-task-detail > p[role=status]")` 换回
`screen.getByRole("status")`（556、575、643 行），并各自留了说明注释；
全库已无 `p[role=status]` / `[role=status]` 残留（`rg` 0 命中）。
`cd web/workbench && pnpm test` → `Test Files 64 passed (64) / Tests 1008 passed (1008)`。

## 实跑清单（全部在被审树 `bea0b674` 上）

| 命令 | 结果 |
| --- | --- |
| `python3 scripts/dev-env/check-operation-coverage.py` | `findings: []`，exit 0 |
| `python3 scripts/dev-env/__old_scan.py`（旧扫描器对照，临时文件） | `findings: 5`，exit 1 |
| `uv run pytest tests/unit/test_operation_coverage.py -q` | **18 passed** |
| `uv run pytest tests/unit/backend -q` | **3158 passed**（与基线一致） |
| `uv run pytest tests/contract -q` | **3799 passed** |
| `pnpm test`（`web/workbench`） | **64 files / 1008 tests passed** |
| `gh pr checks 789` | 20 pass / 2 skipping / 0 fail |
| 变异 M1 / M2 / M3 | 3 / 3 / 2 failed，均为单点 1 hunk，跑完还原 |

## 建议（均不阻塞合并）

- **S1（provenance）**：`docs/audits/...coverage.json` 的 `audited_head` 记成
  `2a3299ba`（= `origin/main`，PR 的 base），而证据是在 `bea0b674` 上重生成的
  （`getWorkbenchStatusV2` 的 tests 列表已不含 `FileTaskHost.test.tsx`，只有修好的树才成立）。
  `differences()` 不校验该字段，所以门禁不报；但 audit map 是人工复核记录，
  head 写了产生不了该证据的提交，建议改成重生成时的 head（或合并时的实际 head）。
- **S2（残留宽松点）**：第 3 条 `\.<method>\(` 仍吃 `...view(` 展开运算符
  （`editor-session.test.ts` 5 行），而 PR 的 docstring 与 PR 描述说这一形态是
  「真正调用执行该 operation 的方法」。目前无害（`getTranscript` 另有真调用），
  若要名副其实，可加一个负向断言 `(?<!\.)\.<method>\(`（只排除 `..view(`，
  保留 `client.view(`/`).view(`/`?.view(`）。建议单独开 issue，不与本 PR 混。
- **S3（spy 规则边界写清）**：规则允许「装了不调用、也不断言」的装饰性 spy，
  当前 0 个 operation 依赖这种情形。建议在 `test_pattern()` docstring 里补一句
  「本形态只证明方法名绑到真实对象；调用是否发生由测试断言，扫描器不做静态证明」，
  避免下一位读者把 spy 当成调用证明。
- **S4（相邻缺陷，本 PR 之外）**：`is_test_candidate()` 用绝对路径判
  `path.parts[0] == "tests"`，对 `Path('/.../tests/x.py')` 永远为假，
  结果是 `tests/` 下 **677 个 Python 测试全部不进证据集**（实测
  `is_test_candidate(abs)=False`、`is_test_candidate(相对路径)=True`；
  65 个候选文件里 `.py` 为 0）。这是旧代码就有的缺陷，本 PR 未触及；
  它恰好屏蔽了 `.revise(` 这类 Python 领域对象同名方法的假阳性，
  但同时也让 Python 侧真证据无法计入。建议另开 issue 修，并在修之前不要宣称
  「证据覆盖所有测试」。

## 只能依赖作者陈述的点

无实质性依赖。我独立复现了作者报的全部数字（147→138、9 删、5 个 operation、
38 spy-only、12 夹具键、15→10、5 个 tests_drift、18/3158/3799/1008）。
两处作者未在 PR 里写、由我补证的事实：`#786` 改用属性选择器是**为绕开扫描器**
（main 上 `FileTaskHost.test.tsx` 的原注释自述如此，非我推断）；`--check` 并不存在。

## 过程时间线

| 时间（2026-11-15，CST） | 动作 |
| --- | --- |
| 00:41 | `docs/reviews/` 在 `origin/main` 上不存在；`git fetch`，worktree `/private/tmp/sn790` 从 `origin/main`（`2a3299ba`）拉 `review/790-coverage-evidence`，`git diff origin/main...HEAD --stat` 为空 |
| 00:42 | 读 `git show bea0b674 --stat` 与完整 PR diff（4 文件）、issue #788、PR 描述 |
| 00:44 | 建 detached worktree `/private/tmp/sn790-pr` 签出被审 head；`uv sync --locked` 与 `pnpm install --frozen-lockfile` |
| 00:46 | A1：枚举 42 个客户端方法与 65 个测试候选，逐行分类 78 处 `\.<method>\(` |
| 00:47 | workbench vitest 1008 passed；确认 `--check` 不是合法 flag（exit 2） |
| 00:48 | A2：id 规则临时改为排除引号 id，`--write` 临时 map，比对 138→120、0 翻转；拆出 12 fixture-key + 6 其他 |
| 00:50 | A3：38 条 spy-only 分类，6 个 100%-spy-only operation 逐个查断言；C5 旧扫描器 5 个 tests_drift + 探针文件对照 |
| 00:51 | 变异 M1（裸引号回归）→ 3 red；M2（删 spy 分支）→ 3 red；均还原 |
| 00:52 | 变异 M3（成员索引形式）→ 2 red；还原 |
| 00:53 | spy 埋点实验：`status` spy 抛错 → `assistant-session.test.ts` 7/7 红 → 还原 |
| 00:54 | B4 逐条核对 9 条删除；D7 数字与 check；D8 三处回改与 `p[role=status]` 清零 |
| 00:55 | pytest 18 / 3158 / 3799 与 `gh pr checks 789` 收齐；写本报告 |
