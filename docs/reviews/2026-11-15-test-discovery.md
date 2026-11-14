# PR #796 非作者审查：Python 测试证据收录（#792）

- **PR**：`fix/792-test-discovery` → `main`
- **head**：`5be3d43d745b2d4b60e0c8621b54cc125e193d48`（父提交 `d07cd64e`，即 #789 落地后的 main）
- **被修 issue**：`#792`（`check-operation-coverage.py` 的 `is_test_candidate()` 用绝对路径判 `parts[0] == "tests"` 恒假，`tests/` 下 677 个 Python 文件从不进证据集）
- **审查时间**：2026-11-15 01:56–02:30（Asia/Shanghai）
- **审查方式**：独立 worktree `/private/tmp/sn797`，`git worktree add -b review/797-test-discovery origin/main`；被审内容一律用 `git show 5be3d43d:<path>` 读取，另在 `/private/tmp/sn797-base`（detached `5be3d43d`）中实跑与造变异，二者均在结束后复原
- **审查时 `origin/main`**：`d55eebb44878d3fe9135d1b80fd061f8c97e8e30`
- **基准事实**：`docs/reviews/` 此前不存在，本文件为首个审查记录，未覆盖任何既有文档

---

## 结论

**可以合并，但合并前必须先 rebase 并重生成审计 map（见 MF-1）。** 修法方向正确：路径 bug 是真 bug，Python 专属证据规则确实按语言形状重写了 `#788` 的假阳性判据，而不是把 TS 的宽松匹配换个后缀。核心数字（`138 → 358`、`52/42/10` 不变、`+220/−0`、`51/52` operation 增加、0 个 pending 变 bound）**全部独立复现，无一虚报**。

两项核心判断：

- **「+220 里有没有假阳性」**：有，**恰好 2 条，且正是作者自报的那 2 条**。我按 handle 证据的 11 个来源文件做了**穷举复核**（不是抽样）：`77` 条 handle-only 条目全部可追到真调用面，唯二例外即作者已登记的两条。除此之外没有发现未申报的假阳性。另有 5 条属于「真但不是请求」（`NO_SERVICE` 豁免集 3 条 + `route_conformance` 的 live 路由报告 2 条），强度弱于请求但仍指向真实挂载面，不构成假阳性。
- **「有没有真实证据被漏掉」**：**有，而且比作者自报的「`httpx.request` 未覆盖」更宽**。`requests_route()` 只认「`.verb("字面量"` 紧跟在同一调用点」这一种写法，而 `tests/unit/backend` 的主流写法是把路径放进模块常量或元组表再 `c.request(method, path)`／`c.get(PATH + "?query")`，整类被判为「无请求」。已确证的真实漏检实例：`getVoiceRevisionV2` ← `test_workbench_voices_http.py`（真 ASGI 请求，四个 leg 全部落空，0 条证据）、`getMeeting/createMeeting/sealMeeting` 类 ← `test_workbench_http_meetings.py`、`updateTeleprompterState/getTeleprompterState` ← `test_workbench_http_teleprompter.py`。**但这一漏检今天不改变任何状态**（见 §3.2 与 §4 的因果说明），所以它不影响本 PR 的结论，只影响 map 作为「证据清单」的完备性。

另外发现一个**合并后才会暴露**的问题：本 PR 的 map 是在 `d07cd64e` 上生成的，而 `origin/main` 已前进一提交（`d55eebb4` 新增 `tests/web/render-refusal.behavior.spec.mjs`），因此在**当前 main 上 `check` 模式 exit 1**（7 条 `tests_drift`）。这不是逻辑错误，是 rebase 未跟上，但会让 `make operation-coverage-check` 在合并后立刻变红，属必须修。

---

## 一、A1：`+220` 的假阳性复核

### 1.1 分类与作者表格对齐

我用 PR 自己的 `python_evidence()` 逐条回判 220 条新增，得到与作者表格一致的分布（唯一差异：作者的「句柄 76」+「`.mjs` 1」在我这里合并计为 `handle 77`）：

| leg 组合 | 条数 | 作者表格 |
| --- | --- | --- |
| 仅请求 | 108 | 108 ✓ |
| 仅句柄 | 77（含 `.mjs` 1） | 76 + `.mjs` 1 ✓ |
| 绑定 + 句柄 | 19 | 19 ✓ |
| 请求 + 句柄 | 6 | 6 ✓ |
| 请求 + 绑定 + 句柄 | 5 | 5 ✓ |
| 句柄 + live | 5 | live 5 ✓ |
| 合计 | **220** | 220 ✓ |

`−0`：`ops with decrease = []`，`consumers`/`binding` 全 52 条零变动（`consumers` 恒 58 条），确认这张表只是「测试腿」的展开。

### 1.2 「精确 operationId 句柄」那 76 条：真证据还是「提了名字」？

这是本卡的关键问题。`#788` 的教训是「名字不算」。我把 handle-only 的 **11 个来源文件全部读完**（穷举，不是抽样），逐文件判：

| 条数 | 文件 | 句柄出现的位置 | 判定 |
| --- | --- | --- | --- |
| 37 | `tests/unit/backend/test_workbench_http_degradations.py` | `DEGRADATIONS` 冻结表（34 条）+ `NO_SERVICE` 豁免集（3 条） | **真**：`_sweep()` 从 live app 读 `_routed(app)`，对**每个**路由 `await client.request(...)` 真跑；34 条是 sweep 观测到的 `(operation, status)` 对；3 条（`createRenderUnit`/`getWorkbenchStatus`/`getWorkbenchStatusV2`）同样被请求驱动，只是不进「必须调到 service」的断言——**弱一档但仍真** |
| 17 | `tests/contract/test_workbench_http_errors.py` | `OPERATIONS` / 会议 operation 的 parametrize 表 | **真**：`validate_response(operation, status, value)`、`validate_request(operation, value)` 以该 operationId 为实参真调生成契约边界 |
| 8 | `tests/unit/backend/test_workbench_http_error_fallback.py` | `UNEXPECTED`/`MALFORMED` 元组首元素 | **真**：元组带 `("getAssistantTurn", "GET", "/v1/workbench/assistant-turns/turn_1")`，`await c.request(method, path, ...)` 是真实请求（只是请求腿的调用形状没覆盖到） |
| 4 | `tests/unit/backend/test_workbench_http_versions.py` | `doc = app.openapi()` 后 `assert ops == {...}` | **真**：对 live app 的 operationId 集合断言这 4 个操作确实挂载 |
| 2 | `tests/contract/test_workbench_download_errors.py` | `@parametrize("operation", [...])` | **真**：`validate_response(operation, status, ...)` 真调 |
| 2 | `tests/unit/backend/test_workbench_http_management.py` | `{path: operationId}` 表 | **真且最强**：既断言 `schema["paths"][path]["get"]["operationId"] == operation`，又 `await c.get(path.format(...))` 发真实请求（`.format()` 形状导致请求腿漏检） |
| 2 | `tests/contract/test_workbench_v2_migration.py` | `[(op, 200), ...]` parametrize | **真**：`validate_response(operation, status, ...)` 真调 |
| 2 | `tests/unit/test_route_conformance.py` | `rows["getVoiceRevision"].route.statuses == ...` | **真但非请求**：`rows` 来自 `build_report(ROOT)` 的 live 路由报告，断言的是挂载路由的 statuses，未发请求 |
| 1 | `tests/contract/test_workbench_binary_http_bindings.py` | `if example["operation"] == "downloadBookExport"` | **真**：该比较只为选 fixture，随后 `validate_response(example["operation"], ...)` 真调生成边界 |
| 1 | `tests/contract/test_render_unit_text_bound.py` | `_DISPATCH_OPERATION = "generateRenderUnitV2"`，仅用于 `assert _DISPATCH_OPERATION in description` | **不算**（作者自报 ✓，见 §5） |
| 1 | `tests/fixtures/contracts/workbench-streaming-http.mjs` | `readEvents("streamExportEvents", ...)` | **真**：由 `test_workbench_streaming_tcp.py:188` 以 `node` 拉起、对真服务跑的可执行探针，调用生成读写器 |

### 1.3 抽查清单（18 条，含每条的实际证据行）

| operation | 文件 | 命中 leg | 证据行 | 判定 |
| --- | --- | --- | --- | --- |
| `cancelBookExport` | `tests/integration/test_workbench_exports_http.py` | req | `await client.post(...)` 字面量路径 | 真 |
| `createBook` | `tests/integration/test_workbench_exports_http.py` | req | 同上 | 真 |
| `cancelAssistantTurn` | `tests/integration/test_workbench_assistant_http.py` | req | `await client.post(...)` | 真 |
| `downloadSavedAsset` | `tests/unit/backend/test_workbench_http_downloads.py` | req+bind+handle | L100 表 + `validate_request("downloadSavedAsset", ...)` | 真 |
| `getVoiceRevision` | `tests/integration/test_workbench_runtime_voice_catalogue.py` | req+bind+handle | L70 `validate_response("getVoiceRevision", 200, ...)` | 真 |
| `streamExportEvents` | `tests/contract/test_workbench_streaming_tcp.py` | req+bind+handle | L150 `validate_request("streamExportEvents", request)` | 真 |
| `getVoiceRevisionV2` | `tests/unit/backend/test_workbench_http_management.py` | live+handle | L63 `schema["paths"][voice_v2]["get"]["operationId"] == ...` + 后续真请求 | 真 |
| `listVoices` | `tests/unit/backend/test_speech_voice_catalogue_http.py` | live+handle | L14 表 + live 断言 | 真 |
| `getProject` | `tests/contract/test_workbench_http_bindings.py` | bind+handle | L168 `validate_http_request("../../private", "getProject", {})`（unknown-profile 负例，抛错前未用到该 operation） | **不算**（作者自报 ✓） |
| `generateRenderUnitV2` | `tests/contract/test_render_unit_text_bound.py` | handle | L55 定义 + L140/151 对契约 description/表格文本断言 | **不算**（作者自报 ✓） |
| `downloadBookExport` | `tests/contract/test_workbench_binary_http_bindings.py` | handle | L23 选 fixture → L55 `validate_response(example["operation"], ...)` | 真 |
| `getVoiceRevision` | `tests/unit/test_route_conformance.py` | handle | L212 `rows[...].route.statuses`（live 报告） | 真但非请求 |
| `createMeeting` | `tests/contract/test_workbench_http_errors.py` | handle | L124 parametrize → `validate_response(operation, ...)` | 真 |
| `getTranscriptRevision` | `tests/unit/backend/test_workbench_http_versions.py` | handle | L347-355 live `ops == {...}` | 真 |
| `getAssistantTurn` | `tests/unit/backend/test_workbench_http_error_fallback.py` | handle | L50 元组 → `c.request(method, path)` | 真 |
| `downloadBookExport` | `tests/unit/backend/test_workbench_http_management.py` | handle | L68 表 → live 断言 + `c.get(path.format(...))` | 真 |
| `getRenderUnit` | `tests/contract/test_workbench_v2_migration.py` | handle | L62 parametrize → `validate_response(operation, ...)` | 真 |
| `streamExportEvents` | `tests/fixtures/contracts/workbench-streaming-http.mjs` | test_pattern | L58 `readEvents("streamExportEvents", ...)` | 真 |

**结论**：`+220` 中 **218 条为真实调用面**，2 条只提名字；**未发现第 3 条未申报的假阳性**。

---

## 二、A2：`python_evidence()` 三种「不算」变异的实测

在真测试文件 `tests/unit/backend/test_workbench_http_versions.py` 末尾各追加 1 hunk（该文件原本对 `getBook` 无任何证据），跑 `python_evidence()` 后 `git checkout --` 复原。基线：`evidence=False bind=[] live=[] req=False handle=False`。

| 变异 | 内容 | 期望 | 实测 |
| --- | --- | --- | --- |
| (a) 注释里的绑定调用 | `# validate_response("getBook", 200, {"body": payload})` | 不算 | `evidence=False`，四 leg 全 False ✓ |
| (b) `<id>-variant` 夹具键 | `_FIXTURE = _example("getBook-invalid-request")["body"]` | 不算 | `evidence=False`，`handle=False` ✓ |
| (c) docstring 提 operationId | `def test_zz(c): """getBook""" ...` | 不算 | **`evidence=True`（handle=True）✗** |

(c) 复现条件的精确边界（`python_operation_handles` 的 `previous` 令牌判定）：

| 写法 | 是否被计入 | 说明 |
| --- | --- | --- |
| `def t(): """getBook"""` | **计入** | docstring 前一个有效令牌是 `:`（在 `opens_a_value` 集合内） |
| `class T: """getBook"""` | **计入** | 同上 |
| 模块首行 `"""getBook"""` | 不计 | `previous is None` |
| `"""The getBook route ..."""` | 不计 | 整串 ≠ operationId |
| `# getBook` / `# validate_response("getBook", ...)` | 不计 | COMMENT 被过滤 |
| `assert "getBook" in text` | 不计 | 前令牌是 `assert` |
| `_example("getBook-invalid-request")` | 不计 | 精确成员判定 |

即：**只要 docstring 的整串内容恰好等于 operationId，就会被当成句柄**。仓库实测该形状 **0 处**（`grep '"""<id>"""'` 全仓无命中），所以今天是纯边界问题，不是实际假阳性；但作者在提交信息里写「docstring … 不算」是不精确的，且没有任何一条测试钉住「docstring = 整串 operationId」这个形状（`test_python_comment_and_docstring_mention_is_not_evidence` 只用了「docstring 里含 id」的散文形式，自然通过）。

---

## 三、B3：新规则漏掉的真实 Python 证据

### 3.1 `request_paths()` 的实际判据

它要求「`.verb(` 之后**紧跟**引号字面量」，并对 f-string 的花括号做深度掩码。因此以下四种真实写法全部漏检：

| 机制 | 实例 | 是否被别的 leg 兜住 | 影响 |
| --- | --- | --- | --- |
| `.request("GET", f"...")`（verb 为字面量） | `tests/unit/backend/test_workbench_voices_http.py:167` | **否** | `getVoiceRevisionV2` 该文件 **0 条证据**（`python_evidence=False`，`requests_route=False`，handle/bind 均 False） |
| `.request(method, path)`（verb 与 path 都是变量，来自元组表） | `tests/unit/backend/test_workbench_http_error_fallback.py:135` | 是（handle） | 8 条靠 handle 记上 |
| 路径放模块常量再 `c.get(PATH + "?query")` | `tests/unit/backend/test_workbench_http_meetings.py:98,233`（`PATH = "/v1/workbench/meetings/meeting-demo"`）；`..._teleprompter.py` 13 次调用中 12 次首参非字面量 | 部分 | `getMeeting`/`updateTeleprompterState` 等的该文件证据缺失 |
| 首段为变量插值 `f"/{version}/workbench/voices/..."` | `tests/unit/backend/test_workbench_voices_http.py:150` | 否 | `getVoiceRevision` 该文件证据缺失 |

`conftest.py` 一项：`tests/conftest.py` **不含任何 HTTP 请求**，无影响。

### 3.2 为什么「0 影响」成立

三点因果，全部实测：

1. **状态判定里测试腿不可能翻盘**：`derive()` 的 status 是 `bound if bound else consumer_pending if not consumers else test_pending`。10 个 pending 的 `consumers` 全为 `[]`，因此无论测试腿补多少条，都只能落在 `consumer_pending`（见 §4）。
2. **没有 test_pending 项**：新 map 里 `status == "test_pending"` 的操作是 **0 个**，所以不存在「因为漏检被误判为测试腿不足」的操作。反例 1 已证明该误判机制本身是通的（§5）。
3. **每个操作仍有非空证据**：`ops with 0 test entries` 为空。

即漏检方向会让 map **少记**真实证据（安全性方向正确），代价是消费端就绪后可能对着已有的测试喊 `test_pending`，需要再补规则。属已知边界，不阻断本 PR。

### 3.3 另一个方向（顺带核实）：最宽松写法能「多记」多少

我用「完整引号字面量 + 段数与静态段全等」的严格匹配扫全仓，得到 100 对「路由字面量出现在未被记入的文件里」。其中相当部分是**契约路径表**（如 `tests/contract/test_p0_public_contracts.py` 的路径清单）而非请求，属**不该计入**的「提名字」；剔除后剩余部分与 §3.1 的机制一致。这说明当前规则在「不误记」上做对了，代价是「不全记」。

---

## 四、C4：10 个 pending 是否全部卡在消费端

| operation | status | consumers | tests | Δtests | binding 全绿 |
| --- | --- | --- | --- | --- | --- |
| `createRenderUnit` | consumer_pending | 0 | 5 | +3 | ✓ |
| `generateRenderUnit` | consumer_pending | 0 | 3 | +2 | ✓ |
| `getChapter` | consumer_pending | 0 | 2 | +2 | ✓ |
| `getRenderUnit` | consumer_pending | 0 | 3 | +3 | ✓ |
| `getTranscriptRevision` | consumer_pending | 0 | 3 | +3 | ✓ |
| `getVoiceRevision` | consumer_pending | 0 | 6 | +6 | ✓ |
| `getWorkbenchStatus` | consumer_pending | 0 | 13 | +13 | ✓ |
| `listRenderUnits` | consumer_pending | 0 | 2 | +1 | ✓ |
| `streamExportEvents` | consumer_pending | 0 | 3 | +3 | ✓ |
| `updateRenderUnit` | consumer_pending | 0 | 4 | +2 | ✓ |

10/10 `consumer_pending`，`consumers` 全空，`binding` 三项全 True，测试证据全部有增加。**「0 个 pending 变 bound」由代码结构决定**：`status` 先看 consumers，测试腿再强也无法越过空 consumers。作者的因果解释成立。

---

## 五、C5：反例 1 复现

在 PR 树与 `origin/main` 树上同时删掉 `web/workbench/src/teleprompter/teleprompter-state.test.ts`，用两版脚本各跑一次 `derive()`：

```
=== 修前（origin/main 脚本 + 删 TS 测试）===
  getTeleprompterState: tests=[] consumers=['web/workbench/src/manuscripts/workbench-client.ts'] status=test_pending

=== 修后（PR 脚本 + 删 TS 测试）===
  getTeleprompterState: tests=['tests/unit/backend/test_workbench_http_degradations.py'] consumers=[...] status=bound
```

与作者陈述逐字一致。它证明的是「Python 测试腿现在能把一个 consumer 就绪的操作抬到 `bound`」——即修的是测试腿方向，且该方向可达。复原后 `git diff` 为空。

---

## 六、D6：留在 map 里的 2 条「不算」

- `getProject ← tests/contract/test_workbench_http_bindings.py`：`validate_http_request("../../private", "getProject", {})` 位于 `pytest.raises(ValueError, match="Unknown HTTP contract profile")` 内，因 **profile 未知**在读取 operation 之前就抛出，operationId 只是实参。**不是证据**。
- `generateRenderUnitV2 ← tests/contract/test_render_unit_text_bound.py`：`_DISPATCH_OPERATION` 只用于断言契约文档 description / 表格文本包含该名字。**不是证据**。

**判断：留在 map 里可接受，但必须把边界写进被审对象本身（模块 docstring 或 map 的 note），不能只写在 PR 描述里。**

理由（实测）：map 是 `--write` 从代码重新推导的，**手工删除无法稳定存在**——我把这两条从 map 副本删掉后跑 `check`，立刻得到 2 条 `tests_drift`（`generateRenderUnitV2`、`getProject`）。要真正删掉只能改脚本（例如让绑定规则跳过 `pytest.raises` 内的调用，或要求 operationId 是解析后的实参），那超出本卡范围且会引入误伤。因此「如实记录 + 声明边界」是当前结构下唯一自洽的选择，与 `#788` 的精神不冲突（`#788` 禁止的是**未经声明**地让名字充当证据）。

**但作者把这条边界只写在提交信息/PR 描述里**，而 `scripts/dev-env/check-operation-coverage.py` 的 `python_evidence()` docstring 仍写着「三种都必须是真调用」，读者看代码会以为这两条不存在。建议 MF-3。

---

## 七、E7：数字与门禁

### 7.1 数字核对（全部独立复现）

| 项 | 作者 | 实测 |
| --- | --- | --- |
| 测试证据条目 | 138 → 358 | **138 → 358** ✓ |
| 新增/减少 | +220 / −0 | **220 / 0** ✓ |
| operations / bound / pending | 52 / 42 / 10 | **52 / 42 / 10** ✓ |
| consumers 总数 | 不变 | **58 不变**，52 条 consumers 逐条相同 ✓ |
| binding 标志 | 不变 | **52 条全部相同** ✓ |
| 条目增加的 operation | 51/52 | **51/52**，唯一无新增 = `updateTeleprompterState` ✓ |
| pending → bound | 0 | **0** ✓ |
| `tests/` 下 Python 文件数 | 677 | **677** ✓ |
| `--write` 可重生成 | 未声明 | 在 PR 自身 base 上 `--write` 后与新 map **逐字节相同**（仅 `audited_head` 不同）✓ |

### 7.2 `check` 模式退出码——**这里有问题**

```
# 在 PR 自身 base（5be3d43d）上：
$ python3 scripts/dev-env/check-operation-coverage.py
{"operations": 52, "bound": 42, "pending": 10, "findings": []}
EXIT=0                ← 作者陈述成立

# 在当前 origin/main（d55eebb4）上叠加本 PR 的三个文件：
$ python3 scripts/dev-env/check-operation-coverage.py
{"operations": 52, "bound": 42, "pending": 10, "findings": [
  {"code": "tests_drift", "id": "generateRenderUnitV2", ...},
  ... 共 7 条 ...
]}
EXIT=1                ← 合并后必然变红
```

差异来源已定位到单点：`origin/main` 在 PR base 之后新增了 `d55eebb4`（#793），该提交加入 `tests/web/render-refusal.behavior.spec.mjs`，它命中 7 个操作的 `test_pattern()`，而 PR 的 map 生成时该文件还不存在。逐条 delta：

```
generateRenderUnitV2: +['tests/web/render-refusal.behavior.spec.mjs']
getBook:              +[同]
listBooks:            +[同]
listChapters:         +[同]
listProjects:         +[同]
listRenderUnits:      +[同]
listVoices:           +[同]
```

`make operation-coverage-check` 是本地门禁目标（`Makefile:18`，未接进 `.github/workflows/ci.yml`），所以 CI 仍绿；但只要有人在合并后跑该目标就会红。修复成本 = rebase + 一条 `--write`。

---

## 八、E8：门禁自测与变异

- `uv run pytest tests/unit/test_operation_coverage.py -q` → **30 passed**（PR base）；`origin/main` 版本基线 **18**（跑出的 17 passed + 1 因 map 被换成 PR 版而失败的 `test_committed_map_is_current`），**+12 条新测试**，与作者「29 passed」的说法不符——**提交信息里的 29 是少算了一条**（实际 30）。
- `tests/unit/backend -q` → **3158 passed**（44.99s）
- `tests/contract -q` → **3799 passed**（147.97s，需先 `pnpm install`；未装 node 依赖时 8 条 TS/node 用例报 `ERR_MODULE_NOT_FOUND: openapi-fetch`，纯环境问题，与 PR 无关）
- `gh pr checks 796` → **无 failure**（2 项 `skipping`；含 `Python: tests, coverage and package build`、`Gate: Python execution and static checks`、六个 Integration 分片）

变异验证（改脚本 → 看哪条测试失败 → `git checkout --` 复原；每次均确认 `git diff` 为空）：

| # | 变异 | 结果 |
| --- | --- | --- |
| M1 | 把 `is_test_candidate` 改回 `path.parts[0] == "tests"` | ✓ `test_absolute_paths_below_tests_are_discovered` 失败 |
| M2 | 去掉 `GATE_SELF_TEST` 排除 | ✓ `test_gate_own_unit_test_is_not_evidence_of_its_operations` 失败 |
| M3 | 句柄判定改成子串包含 | ✓ `test_python_fixture_key_is_not_an_operation_handle` 失败（连带 1 条） |
| M5 | `requests_route` 忽略 verb（五个 verb 并集） | ✓ `test_python_request_with_another_verb_is_not_evidence` 失败 |
| M4 | 把 `request_paths` 换成「读到下一个同引号为止」的朴素实现（丢掉花括号掩码） | ✗ **30 passed——变异存活** |

M4 值得单列：`test_python_request_to_a_sibling_route_is_not_evidence` 用的是 `f"/v1/workbench/books/{book['id']}/chapters"`（内层单引号、外层双引号），朴素读取器也能读全，靠**段数不等**就把兄弟路由挡掉了，根本没触及「花括号掩码」这条理由。只有 PEP 701 的同引号嵌套 `f"/v1/workbench/books/{book["id"]}/chapters"` 才会让朴素实现退化成误判，而这正是脚本 docstring 声称要防的情形——该形状**没有任何测试钉住**（仓库当前也不用这种写法，属于前瞻防御）。

---

## 九、必须修清单

| # | 项 | 依据 | 建议 |
| --- | --- | --- | --- |
| **MF-1** | **rebase 到当前 `origin/main` 并 `--write` 重生成 map** | §7.2：当前 main 上 `check` exit 1，7 条 `tests_drift`，全部来自 `d55eebb4` 新增的 `render-refusal.behavior.spec.mjs`。合并后 `make operation-coverage-check` 必红 | `git rebase origin/main` + `python3 scripts/dev-env/check-operation-coverage.py --write` + 复核新增 7 条 `.mjs` 证据真伪（该文件是真实浏览器规格，应计入） |
| **MF-2** | **修正提交信息里的自测数量** | §8：实际 30 passed，提交信息写 29 | 改提交信息或在 PR 描述更正；避免审计记录与其他审查记录对不上 |
| **MF-3** | **把「2 条不算」的边界写进代码 docstring，而不只写在 PR 描述** | §6：`python_evidence()` 的 docstring 仍称「三种都必须是真调用」，而实际 map 里有 2 条是「名字」；读者只看代码会被误导 | 在 `python_evidence()` 或 map 生成逻辑处注明：绑定规则无法区分「参数是 operationId 但调用预期抛错」的负例；句柄规则无法区分「赋值给常量后仅作文本断言」；二者各 1 例已登记在 map 中并计划随规则收紧移除 |
| **MF-4** | **（建议）补一条钉住「docstring 整串 = operationId」不算的证据** | §2：`"""getBook"""` 当前**会**被计为句柄，与提交信息「docstring 不算」矛盾；仓库现有 0 例，属前瞻边界 | 要么修规则（排除 `previous == ":"` 且后随 NEWLINE/ENDMARKER 的孤立字符串表达式），要么在 docstring 中把结论降级为「散文式 docstring 不算」，并补测试固定所选语义 |
| **MF-5** | **（建议）补一条钉住 f-string 花括号掩码的测试** | §8 M4：朴素读取器能通过全部 30 条测试，掩码这条理由未被测试保护 | 在兄弟路由用例里改用同引号嵌套 `f"...{book["id"]}/chapters"`，使朴素实现必然误判，从而钉住掩码 |
| **MF-6** | **（建议）在模块 docstring 补记请求腿的已知漏检形状** | §3.1：作者只自报 `httpx.request` 未覆盖，实际至少还有 `.request(verb, path)`、路径存模块常量、helper 传参、首段变量插值四种，且主流写法正是后几种 | 在 `requests_route()` docstring 列明「只识别调用点紧跟字面量」的限制与后果（map 少记、不会多记），供后续规则收紧时对照 |

---

## 十、只能依赖作者陈述、未能独立验证的点

1. **提交信息中「ruff check/format、mypy --platform linux 全绿」**：我复核了 `uv run --no-project ruff format --check`（2 files already formatted）与 `ruff check`（All checks passed），**mypy 未独立跑**（未在本 worktree 装 mypy 配置跑全量，采信 CI 的 `Python: lint, types and contract compatibility` pass）。
2. **「逐条复核 220 条新增，218 真实 / 2 不算」**：我用穷举 handle 来源 + 18 条抽查独立得到同一结论，但我的句柄判定是「读代码人工判真伪」，与作者的人工判据不完全可复现；若有第 2 位审查者得出不同切分，以代码为准。
3. **`d55eebb4` 的 `render-refusal.behavior.spec.mjs` 证据真伪**：该文件是 PR 之后进 main 的浏览器规格，我确认它命中 7 个操作但**未逐条审它的断言是否覆盖那 7 个操作**（超出本 PR 范围），MF-1 重生成时需要一并复核。
4. **`gh pr checks 796` 的 CI 运行对应 head `5be3d43d`**（`gh pr view` 的 `headRefOid` 一致），但 CI 跑的是 PR 自己的 base，无法替我验证「合并后的 main」是否绿——这正是 MF-1 要处理的问题。

---

## 附：过程记录

| 时间 | 动作 | 结果 |
| --- | --- | --- |
| 01:56 | `git fetch`，确认 `docs/reviews/` 不存在；`git worktree add /private/tmp/sn797 -b review/797-test-discovery origin/main` | 分支基于 `d55eebb4`，`git diff origin/main...HEAD --stat` 为空 ✓ |
| 02:00 | 读 `git show 5be3d43d:scripts/dev-env/check-operation-coverage.py`（592 行）+ 测试 diff（+164 行，12 条新测试） | 发现 `except X, Y:` 是 PEP 758 语法，项目 `requires-python = ">=3.14,<3.15"`，**不是问题** |
| 02:03 | PR 三文件在 origin/main 上跑 `check` | exit 1，7 条 `tests_drift` → 定位为 base 落后一提交（MF-1） |
| 02:05 | `git worktree add --detach /private/tmp/sn797-base 5be3d43d` 跑 `check` | exit 0、findings `[]` ✓（作者陈述成立） |
| 02:08 | 枚举 +220 并按 leg 分类 | 108/77/19/6/5/5 = 220，与作者表格一致 |
| 02:10 | 穷举 handle-only 的 11 个文件，逐文件判真伪 | 76 真 + 1 不算；另 3 条 NO_SERVICE、2 条 route_conformance 属弱但真 |
| 02:13 | A2 三变异（真文件追加 hunk 后复原） | (a)(b) 不算 ✓；(c) docstring 整串 = id **计入** ✗ |
| 02:15 | C5 反例 1：两版脚本 + 删 TS teleprompter 测试 | 修前 `test_pending` / 修后 `bound` ✓ 逐字复现 |
| 02:18 | B3：`.request(` 探测器 + 常量表 + 首段插值排查；`conftest.py` 无请求 | 确证 `getVoiceRevisionV2 ← test_workbench_voices_http.py` 0 证据等；因 consumers 全空，0 状态影响 |
| 02:22 | E7 `--write` 重生成对比 | 除 `audited_head` 外逐字节相同；52/42/10、358 ✓ |
| 02:24 | E8 变异 M1/M2/M3/M5 各有对应测试失败；M4 存活 | 4 条被钉住，1 条未钉住（MF-5） |
| 02:26 | `pytest tests/unit/test_operation_coverage.py` / `tests/unit/backend` / `tests/contract` | 30 / 3158 / 3799 passed |
| 02:28 | `gh pr checks 796` | 全部 pass |
| 02:32 | 复原两套工作树（`git restore` 全部改动），确认 `git diff` 为空 | 无变异外流，未 push 任何变异 |
