---
title: PR #746 非作者审查：渲染单元编辑器的两个文本上限（#745）
status: active
---

# PR #746（`fix/745-editor-bounds`）非作者审查记录

被审对象：PR #746《fix: 单元编辑器显示两个文本上限并在拒绝时指名超界原因 (#745)》，
head `6b058dcf64e18be63223e1a373c711dbe9b55ba5`（父提交 `4514b65b`），单 commit，
`git diff --stat 4514b65b 6b058dcf` = 4 文件 `+268 / −17`，全部在 `web/workbench/src/books/`：

```text
web/workbench/src/books/RenderUnitPanel.test.tsx   |  87 ++++++++++
web/workbench/src/books/RenderUnitPanel.tsx        | 108 +++++++++++++----
web/workbench/src/books/render-unit-bounds.test.ts |  58 ++++++++
web/workbench/src/books/render-unit-bounds.ts      |  32 +++++
```

审查方式：独立 worktree `/private/tmp/sn747`（分支 `review/747-editor-bounds`，**基于 `origin/main`**）
只放本报告；被审内容用 detached 快照 worktree `/private/tmp/sn747-pr`（`6b058dcf`）读取与实跑；
全部变异在快照内完成并还原，未 push 任何变异。系统 node `v26.12.1`。

## 结论

**需修改后合入。** 十二条事实性判定成立，一条 P1 语义缺陷成立：

1. 「422 无可区分原因」**成立**（`ErrorBody` 无 reason，`param` 恒 null，message 是固定常量）；
   补齐一点作者没写的更硬证据：上游契约**本来就带可分位**（`TtsJobInput.text` 的 description
   写明超界在异步入口是 `422 unsupported_parameter`、在 `/v1/audio/speech` 是 `400` 且 `param: input`），
   是工作台适配器把 `400/401/403/415/422` 全部折叠成同一个固定 422，才让前端只能猜。
   所以选 (a) 在当期范围是对的，(b) 的动机比他写的更强，但仍属公共协议变更，不适合塞进本卡。

2. 「前端指名会不会误栽」——**会，而且不是理论问题**。作者用「派发的那份已存文本」做判据是对的
   （草稿不参与，实测确认），但他用的**计数单位是 UTF-16 码元，而契约、AJV、`MAX_SPEECH_TEXT_CHARS`
   三处都按 Unicode 码点计数**。于是对星形平面文本（emoji、扩展 B 汉字），前端会**多算**：
   2049 个 emoji = 4098 个 UTF-16 码元、但只有 2049 个码点，服务端**能**一次派发；一旦上游因
   `voice_not_published` 之类返回 422，界面就会指名「该单元文本 4098 码元超过一次派发上限 4096，
   因此本次派发被拒绝」——把绑定/发布问题栽给文本长度。已用组件探针复现（见 §2.1）。

纯 BMP 文本（≥4097 码点）下，指名的确**结构上安全**：`PublicSpeechRenderJobs.submit()` 先本地
`parse("JobCreateRequest", body)`，超界即 `raise RenderSubmissionRejectedError`，请求根本到不了上游，
故 4097 个 ASCII 字符被 422 拒，原因必然是文本。这一点作者没说、也没测，但它恰好是他方案能成立的
唯一支点，且只对「码点 ≥ UTF-16 码元」的 BMP 文本成立。

## 1. 「422 是否真的无可区分原因」（A1 / A2）

### 1.1 A1 成立

| 证据 | 位置（`4514b65b` = 本 PR 基点） | 结论 |
| --- | --- | --- |
| `except RenderSubmissionRejectedError: return _error("render_submission_rejected", rid, operation)` | `backend/src/speechnexus_backend/workbench/interfaces/http_render_units.py:203-204` | 只有单一映射，无携带原因的包装 |
| `"render_submission_rejected": (422, "invalid_request_error", "The pinned render unit cannot be dispatched as a speech Job.")` | `backend/src/speechnexus_backend/workbench/interfaces/http_versions.py:151-155` | message 是**固定常量**，不含长度、字段名或子原因 |
| `_error()` 固定构造 `code/type/message/param: None/request_id/retryable: False` | `backend/src/speechnexus_backend/workbench/interfaces/http_versions.py:160-183` | 该操作 `param` 恒为 `null`，也没有 `nexus` 恢复块 |
| `ErrorBody`：`additionalProperties: false`，`required` 恰为 `code/type/message/param/request_id/retryable`，`param` 可空，可选 `retry_after_ms`、`nexus` | `packages/contracts/src/speechnexus_contracts/workbench.schema.json:506-543`（`properties` 到 543，`allOf` 自 544 起） | `nexus` 仅被 `operation_in_progress` / `response_not_replayable` 条件要求；无 reason/detail 分支 |

实测 `nexus` 也不可能承载原因：

```text
$ python3 -c "json/workbench.schema.json → $defs.External_shared_errors_schema_json_OperationRecovery"
{"required": ["operation_id"], "properties": {"operation_id": ..., "receipt": ...},
 "additionalProperties": false}
```

即只有 `operation_id` 与 `receipt`，没有任何可读原因位。**A1 判定：成立，作者引用的三处行号准确。**

### 1.2 A2 成立，但「前端 100% 可预测」只在码点单位下成立

`PublicSpeechRenderJobs.submit()` 的两处 `raise RenderSubmissionRejectedError`：

```text
backend/src/speechnexus_backend/workbench/infrastructure/public_speech_render_jobs.py:64-67
    command = parse("JobCreateRequest", body)      # 本地契约解析：text>4096 码点 / model 非法 / voice 非法…
    → raise RenderSubmissionRejectedError
backend/src/speechnexus_backend/workbench/infrastructure/public_speech_render_jobs.py:83-84
    if response.status_code in {400, 401, 403, 415, 422}:
        → raise RenderSubmissionRejectedError
```

上游这五种状态码的**真实来源**可指认到具体码，例如「绑定/声音未发布」：

```text
backend/src/speechnexus_backend/speech/interfaces/http/job_errors.py:90
    "voice_not_published": (422, "invalid_request_error", "The voice revision is not published."),
backend/src/speechnexus_backend/speech/interfaces/http/jobs.py:492-493
    except VoiceNotPublishedError: return job_error("voice_not_published", request_id)
```

于是同一个工作台 422 至少覆盖：文本超过内联上界、model/binding/voice/profile 未发布、401/403 鉴权、
415 格式不支持。**多因收敛成立。**

而「文本超界可在前端 100% 预测」这一条：**只有按码点才成立**。按码点算，超界时本地 `parse` 必然先失败，
请求永不出站，因此该 422 必由文本造成；按 UTF-16 算，前端会把服务端**接受**的文本判成超界（§2.1），
于是「必由文本造成」不成立。这正是本 PR 唯一的实质缺陷。

## 2. 核心：前端指名会不会误栽（B3 / B4 / B5）

### 2.1 B3：会误栽，且可达——星形平面文本

先给出「契约到底按什么单位数」的三重实测（这点决定了 4096 是谁的 4096）：

```text
# 服务端：jsonschema（parse 用的就是它），file.schema.json → $defs/TtsJobInput
$ (jsonschema Draft202012Validator)
' a'*4096                              cp=  4096 utf16=  4096 valid=True
' a'*4097                              cp=  4097 utf16=  4097 valid=False
emoji*2049 (2049 cp, 4098 UTF-16)      cp=  2049 utf16=  4098 valid=True    ← 服务端接受
emoji*4096 (4096 cp, 8192 UTF-16)      cp=  4096 utf16=  8192 valid=True    ← 服务端接受
emoji*4097                             cp=  4097 utf16=  8194 valid=False

# 浏览器侧契约校验器：AJV（packages/contracts/src/profile-runtime.mjs 用的就是它）
[tts] emoji*2049 (utf16=4098) -> true
[tts] emoji*4096 (utf16=8192) -> true
[tts] emoji*4097 (utf16=8194) -> false
[wb ] astral*100001 (utf16=200002) -> true     ← RenderUnitText maxLength 200000 也是码点

# 语音域自身：Python len()（= 码点）
backend/src/speechnexus_backend/speech/domain/speech_synthesis.py:21  MAX_SPEECH_TEXT_CHARS = 4096
backend/src/speechnexus_backend/speech/domain/speech_synthesis.py:90  not 1 <= len(self.text) <= MAX_SPEECH_TEXT_CHARS
```

**三处（契约 / 浏览器校验器 / 语音域）全部按码点；只有 HTML 的 `maxLength` 属性按 UTF-16 码元。**
`renderUnitLength = text.length` 对齐的是 HTML 属性，而它注释里写的「as `maxLength` counts it」与本文件
镜像的**契约** `maxLength` 不是同一件事。

组件探针（`@testing-library/react`，逐字贴输出）：

```text
[B3  ] 5000 BMP 码元 + 422（代表任意上游拒绝）
       → 该单元文本 5000 码元（UTF-16）超过一次派发上限 4096 码元，因此本次派发被拒绝（422 render_submission_rejected），未生成作业。下一步：…
[B3b ] 6 码元 + 同一 422
       → 请求被拒绝。请核对单元文本或声音后再重做。                 ← 泛指，正确
[B3-astral] 2049 个 emoji = 4098 UTF-16 / 2049 码点（服务端接受该文本）+ 422
       → 该单元文本 4098 码元（UTF-16）超过一次派发上限 4096 码元，因此本次派发被拒绝（422 …）
                                                            ↑ 这是假诊断：文本根本没超服务端的上界
```

可达性链条（无需任何后端改动）：`RenderUnitText` 允许 200000 码点 → 一个 2049 个 emoji 的单元合法落库
→ 派发时 `parse("JobCreateRequest")` 通过（2049 ≤ 4096 码点）→ 上游因 `voice_not_published` 返回 422
→ 适配器折叠成 `422 render_submission_rejected` → 面板按 UTF-16 得 4098 > 4096 → 指名文本过长。

补充（对作者公平的判断）：这段代码**只在 `dispatched` 有值且超过阈值时才指名**，因此 6 码元那种
「本地解释不了」的情况确实保留泛指——**B3 的防误栽护栏存在，但它用错了单位，于是护栏在两个码点/码元
不同的地方漏掉了。**

### 2.2 B4：用的是「派发的那份已存文本」，不是草稿——**正确**

`generateRenderUnit(unitId, expectedRevision, key)` 根本不上送文本；服务端取自己存的
`dependencies.text` 内联成一个 Job body。面板因此用 `units.find(id)?.text`（最后一次读到的服务端文本）：

```text
web/workbench/src/books/RenderUnitPanel.tsx:133-149
   * `dispatched` is the text the panel believes the server put in the request body. …
  const over = dispatched === undefined ? null : renderUnitLength(dispatched);
```

两个方向都实测：

```text
[B4a] 已存 5000 码元，草稿改成「短草稿」但未保存 → 派发 → 提示「该单元文本 5000 码元…」     ← 取已存，正确
[B4b] 已存「短正文」，草稿改成 5000 码元但未保存 → 派发 → 「请求被拒绝。请核对单元文本或声音后再重做。」 ← 不诬告草稿，正确
```

陈旧快照也不构成风险：文本变了修订必变，派发带 `expected_revision`，不一致走 409 `revision_conflict`
而不是 422。**B4 判定：成立，且比我预期的更干净。**

### 2.3 B5：`over` 是「全文长度」，不是「超出量」；与输入侧计数一致

`over` 命名像「超出的部分」，实际是全量长度，提示写成「该单元文本 ${over} 码元」所以不会误读；
同一单元输入侧计数与拒绝侧 N **同源同值**（都走 `renderUnitLength`）：

```text
[B5 counter] 已 4098 码元（UTF-16 码元，与 maxLength 同单位） · 一次派发上限 4096 码元 · 源容量上限 200000 码元。超过一次派发上限…
[B5 alert  ] 该单元文本 4098 码元（UTF-16）超过一次派发上限 4096 码元，…
```

内部一致 ✓；但两个数字都用了错单位（§2.1），所以「一致地错」。

## 3. 计数单位与措辞（C6）

`text.length` 与 HTML `maxLength` 同单位（UTF-16 码元）这一点**本身没错**，错在把它当成契约单位：

- 界面上「已 N 码元（UTF-16 码元，与 maxLength 同单位）」：括号里的解释是给开发者看的，不是给编辑
  有声书的人看的；`码元` 是行话，`maxLength` 是 HTML 属性名。**对操作者不可读。**
- 更严重的是它给出的判断是**错的**：输入 4096 个 emoji，界面会说「已 8192 码元…超过一次派发上限，
  不能一次派发」，而服务端会正常派发 4096 个 emoji 的单元。这正是本卡担心的
  「用户按码点数了 4096 个 emoji 却被告知 8192」——**不只是困惑，是被阻止做一件可行的事。**
- 附带后果：`maxLength={200000}` 按码元，星形文本在约 100000 码点就被输入框拦住，而契约允许
  200000 码点（实测 `astral*100001` 通过 `RenderUnitText`）。方向是安全的（不会放进服务端拒收的文本），
  但界面同时写「源容量上限 200000」，会让人以为自己的 100001 字符「没满却打不进去」。

`render-unit-bounds.test.ts` 里那条注释把结论说反了：

```text
// 2049 astral code points are one code point each but 4098 UTF-16 code units:
// a code-point count would say 2049 and wrongly promise a single dispatch.
```

按码点数会说 2049，而服务端**确实**接受这 2049 码点的一次派发（上表实测）。所以「码点计数会错误地
承诺一次派发」不成立；恰好相反：码点计数才是与三处服务端判据一致的那个。

## 4. 契约常量守卫（C7）

测试确实按 JSON pointer 读**生成的** bundle，不是硬编码：

```text
/$defs/RenderUnitText/maxLength                              -> 200000
/$defs/SpeechRequest/properties/input/maxLength              -> 4096
/$defs/TtsJobInput/oneOf/0/properties/text/maxLength         -> 4096
```

改契约数字实测两处都会红：

```text
# file.schema.json: "maxLength": 4096 → 5000
× mirrors the inline dispatch bound both speech entries declare     AssertionError: expected 5000 to be 4096
# workbench.schema.json: "maxLength": 200000 → 100000
× mirrors the source capacity the contract declares for RenderUnit.text   AssertionError: expected 100000 to be 200000
```

（生成产物由 `make contracts-drift` 的 `--check` 守住，所以「改 yaml 不重生成」也不会漏。）
**C7 判定：成立，真能拦住契约漂移。** 唯一脆弱点：`/TtsJobInput/oneOf/0` 是**位置**指针，
`oneOf` 顺序一变就指到别的分支；`/oneOf/*/required` 含 `text` 的那一支更稳。

## 5. 反例与守卫（D8 / D9）

四条反例全部独立复现，红在预期断言，随后还原（快照 worktree 干净）：

| 变异（仅在快照内） | 红在 |
| --- | --- |
| ① 拒绝侧一律回泛指（删掉 `textOverDispatch` 分支） | `× names text over the inline bound when a dispatch is rejected` → 1 failed |
| ② 编辑区 `maxLength={RENDER_UNIT_DISPATCH_LIMIT}` | `× shows the current length against both bounds and names the split step`、`× keeps 200000 code units editable and stops only above the source capacity` → 2 failed |
| ③ 计数改码点 `[...text].length`（`maxLength` 不动） | `× counts UTF-16 code units exactly as maxLength does`、`× counts UTF-16 code units, so astral text is never under-estimated` → 2 failed |
| ④ 去掉两处源容量 `maxLength` | 同上两条 → 2 failed |

D9（防误栽的泛指用例）**存在**：`keeps the generic refusal when the local text does not explain the 422`
（6 码元 + 422 → 「请求被拒绝」且不含「超过一次派发上限」）。**所以 D9 不是「必须补」。**
真正缺的是一条反向用例：**4098 UTF-16 / 2049 码点的文本收到非文本 422 时，界面必须仍走泛指**
（现实现会红，这正是 §2.1 的缺陷，修好后应作为守卫）。

## 6. 既有门禁与范围（E10 / E11 / E12）

### 6.1 `test:behavior` 的处理诚实

- 基点 `4514b65b` 上**确实没有** `test:behavior` 脚本，也没有配置：

  ```text
  $ git show 4514b65b:package.json | grep test:behavior      # 无
  $ git ls-tree -r --name-only 4514b65b | grep playwright.behavior.config.mjs   # 0 行
  ```

  `#741`（`test/740-behavior-specs` @ `1f90e4ba`）合并进 main 是 **14:29:34**，晚于本 PR 的 head 时间
  （`6b058dcf` 14:17:43），所以「基线没有该脚本」在提交时是真的。现在 main 上有了。
- 借入的 4 个文件自洽：`playwright.behavior.config.mjs` 只 import `tests/web/preview-servers.mjs`，
  spec 只 import `./network-lock.mjs`，规格数恰为 4 条，与「实跑 4 passed」一致。
- **确实还原了**：作者 worktree `/private/tmp/sn746` 停在 `6b058dcf`，`git status --porcelain` **空**，
  四个借入文件均不存在；commit 只含本 PR 的 4 个文件，借入痕迹没有进提交。

### 6.2 例数与门禁（在 `6b058dcf` 快照实跑）

```text
根 pnpm test（全部工作区）     web/admin 19 files / 151 tests · web/workbench 63 / 883 · packages/contracts 2241 pass
web/workbench: pnpm test      Test Files 63 passed (63) · Tests 883 passed (883)
基线 4514b65b:  pnpm test      Test Files 62 passed (62) · Tests 873 passed (873)      → +1 文件 / +10 用例
pnpm typecheck                web/workbench、web/shared、web/admin、packages/contracts 全部 Done
pnpm lint                     [PASSED] Design tokens… · ESLint: passed
pnpm format:check             All matched files use Prettier code style!
pnpm build + pnpm test:a11y   52 passed (18.9s)
uv run --no-project ruff format --check .   1847 files already formatted
```

`test:a11y` 的 52 条**不覆盖**新增的 `TextBounds` 段落（`tests/web/*.a11y.spec.mjs` 里不出现 books/渲染单元，
只在 admin/workbench 首页与 raster 目标上跑）。所以「52 passed」是**回归无变化**，不是对新元素的覆盖——
这一点作者没有夸大，但读者容易误读为「新元素过了无障碍门禁」。

另用 axe-core 4.14 对「`<p aria-label>` + `aria-describedby`」这段结构做过一次探针：
`aria-prohibited-attr` **pass**，不构成违规（本以为是问题，实测不是，故不作为发现）。

### 6.3 范围与依赖

- 改动只有 `web/workbench/src/books/` 下 4 个文件；`contracts/`、`backend/`、`scripts/dev-env/` 未碰。
- `uv.lock`、`pnpm-lock.yaml` 未变；`package.json` 未变，**无新增依赖**（`git diff 4514b65b 6b058dcf -- '*.json' '*.yaml'` 为空）。

### 6.4 基线陈旧（不阻塞，但合并前应 rebase）

PR head 落后当时的 `origin/main`（`cdf60a9e`，#741）一个 commit。已用合并树验证合并**不会**回退 #741：

```text
$ git merge-tree --write-tree origin/main 6b058dcf
92c37fca7b35143f0c0e72f0b875781949c63fa4
$ git cat-file -p 92c37fca:tests/web/file-task.behavior.spec.mjs | wc -l   → 282
$ git cat-file -p 92c37fca:package.json | grep -c test:behavior            → 1
```

## 7. 一处事实（F13）

issue #745 正文写的范围 `web/workbench/src/workbench/render-units/` **在仓库中不存在**：

```text
$ find . -type d -name render-units        # 空（排除 node_modules）
$ ls web/workbench/src/books/             # RenderUnitPanel.tsx 在这里
```

PR 正文对此的更正**准确**。

## 8. 必须修清单

### P1（阻塞）：计数单位必须与契约一致，否则「指名原因」不成立

`renderUnitLength` 用 UTF-16 码元，而契约、AJV、`MAX_SPEECH_TEXT_CHARS` 三处都按码点。后果不是措辞
问题，是：对星形平面文本会**虚报超界**（阻止可行的派发），并让「文本过长」这一指名在非文本 422 上
**假诊断**（§2.1 实测）。这正是本卡 B3 的核心风险。

建议最小改法：

1. `renderUnitLength` 改为码点计数（`Array.from(text).length` / `[...text].length`），并使
   `RENDER_UNIT_SOURCE_LIMIT` / `RENDER_UNIT_DISPATCH_LIMIT` 的比较与文案全部随之切换；
2. 文案从「码元（UTF-16 码元，与 maxLength 同单位）」改为契约的说法（`RenderUnitText` 的 description
   本身写的是 "characters"），例如「已 N 字符（与契约 maxLength 同单位）」；
3. `maxLength={200000}` 可保留作浏览器侧护栏，但必须写清它是**浏览器按 UTF-16 码元**计的上限，
   否则星形文本会在「没到 200000」时打不进去；
4. 同步修正 `render-unit-bounds.test.ts` 里那段说反的注释，并把「2049 emoji（4098 UTF-16 / 2049 码点）
   收到非文本 422 时界面**不得**指名文本」加成用例；
5. 一并修正 issue #745 的 AC 第四条（它把「UTF-16 码元」当成契约单位，是本缺陷的上游来源）——
   否则下一位按 AC 实现的人会走进同一个坑。

若坚持保留 UTF-16 展示（作者可自辩：它与输入框行为一致），最低限度也必须让**指名**只在
「码点计数也超过 4096」时发生，并去掉「不能一次派发」这种确定性判断的虚报。

### P2（不阻塞，建议同轮清掉）

1. `problems.textOverDispatch`（`RenderUnitPanel.tsx:27`）与 `problemMessage` 的
   `problem.over === null` 分支**不可达**：唯一生产者（`:146`）保证 `over` 非空。两条几乎一样的长文案
   并存，日后必然漂移；删掉死文案，只留参数化的一条。
2. 契约指针 `/TtsJobInput/oneOf/0/properties/text/maxLength` 依赖 `oneOf` 顺序，改为按
   `required` 含 `text` 推导或直接钉 `TtsJobInput` 的两个分支都断言会更稳。
3. 「超过源容量上限…保存该单元会被契约拒绝」这条分支在真实 UI **不可达**（`maxLength` 已挡住），
   测试是靠 `fireEvent.change` 绕过属性造出来的。保留作防御可以，但测试注释「One code unit more is
   what the source capacity refuses」会被读成「用户能造出这个状态」。
4. 提示里出现机器码 `（422 render_submission_rejected）`：与该文件其它消息（`（404）`、`(409)`）风格
   大致一致，但它是操作者文案；如果保留，建议放在末尾括注而不要嵌在因果句中间。
5. 合并前 rebase 到当前 `origin/main`（已验证合并不回退 #741）。

## 9. 只能依赖作者陈述的点

- `pnpm test:behavior` **4 passed** 的具体运行：我只能证明该配置/spec 在 `1f90e4ba`（现已在 main
  `cdf60a9e`）存在且恰有 4 条、4 个借入文件自洽、且借入痕迹未进提交；无法复现「用本 PR 构建产物跑过一次」
  那一刻的状态（那 4 个文件在借入当天不在 PR 快照里）。
- 根工作区的 `web/admin` 19/151、`packages/contracts` 2241：已复跑，与作者一致（见 §6.2）。CI
  （`Web: lint, types, tests, build and accessibility`、`Python: tests, coverage and package build`）
  在 PR #746 上为 pass。
- 真实后端 + 真实语音底座的端到端 422：与作者一样，我用契约合法 envelope 在组件层复现，未起
  PostgreSQL/网关；§2.1 的 `voice_not_published` → 422 链路是**读代码 + 状态码集合**证明的，未实跑。

## 10. 过程记录与时间线

| 时间（2026-11-14，+08:00） | 动作 |
| --- | --- |
| 14:28 | `git fetch`；`git worktree add /private/tmp/sn747 -b review/747-editor-bounds origin/main`（当时 main = `4514b65b`）；读 `web/AGENTS.md`；node `v26.12.1` |
| 14:29 | 建 detached 快照 `/private/tmp/sn747-pr`（`6b058dcf`）并 `pnpm install --frozen-lockfile`（269 包，292ms，reused 269）；期间 `origin/main` 前进到 `cdf60a9e`（#741，14:29:34） |
| 14:32 | 快照 `pnpm test` → 63/883；断言 A1/A2/B4/B5 的代码证据 |
| 14:33 | 探针：B3 泛指/bmp、B4 草稿两向、B5 码元一致 → 5 passed（首轮 4 条因 `sessionStorage` 未清空而失败，补 `beforeEach` 后通过；这也印证拒绝态会留在会话里） |
| 14:34 | 变异 M1–M4 + 契约漂移 M5a/M5b，逐一实跑并还原 |
| 14:35 | 三重单位实测：Python jsonschema、AJV、`MAX_SPEECH_TEXT_CHARS`；补 B3-astral 探针复现假诊断 |
| 14:36 | `pnpm build` + `test:a11y` → 52 passed；`typecheck` / `lint` / `format:check` / `ruff format --check` 全绿 |
| 14:37 | E10：作者 worktree `git status` 空、借入文件不在、commit 只含 4 文件；D8 四反例二次确认；基线 62/873 |
| 14:38 | 读 issue #745、PR #746 正文，核对 F13 路径与 CI 绿；合并树验证 #741 不回退 |
| 14:39 | `review/747-editor-bounds` 重置到当前 `origin/main`（`cdf60a9e`），只写本报告 |
