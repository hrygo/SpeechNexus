# 工作台 TTS 输入形状修复的非作者审查（PR #688 / issue #685）

> 过程记录文档：保留时间线、独立复现的反例与命令输出。结论面向合并决定，不修改产品代码。
> 审查对象：`fix/686-tts-input`，head `b8962c423ec29fb2caad34f4e0dce156c6fe00ca`（下称 head），基线 `origin/main` = `6b6e2dd4`。
> 文件名沿用任务指定的 `2026-11-13`；实际审查发生在 2026-11-14 凌晨（PR 检查运行于 2026-11-13T17:54Z，即本地 11-14 01:54），下文时间线用本地时间。

## 结论

**有条件通过**（可合并，但需先补两项：`assetText()` 失败分支的回归测试、issue AC 与契约类型层的书面澄清）。

判定依据：缺陷归属判断正确（后端与契约对，工作台错）；修法发出的 body 在真实后端契约校验器与 admission 读取逻辑下成立；本卡最核心的性质「正文不持久化」经独立验证成立且我把它做到比作者更深的程度；未知提交保护未放宽，作者的数字与红因我逐项复现一致。可以上的问题集中在**验证覆盖**与**记录**，不在产品逻辑：PR 新增的整条「素材不可读」网络分支在作者测试中零覆盖（我用变异实测证明该分支当前无任何咬合力）。

## 审查对象与方法

- 独立 worktree：`git worktree add /private/tmp/sn688 -b review/688-tts-fix b8962c42`（主检出停在 `docs/tracker-batch12`，所有内容一律在 worktree 或 `git show <rev>:<path>` 读取）。
- 复现环境：node v26.12.1、pnpm 12.14.0，`pnpm install --frozen-lockfile --prefer-offline`（离线复用 store，未改动 `pnpm-lock.yaml`/`uv.lock`）。
- 交叉语言验证：用主检出 `.venv`（Python 3.14.9 + jsonschema 4.26.0）加载 **本 PR head 的** `packages/contracts/src/speechnexus_contracts`，跑真实生成契约校验器与 admission 读取语义镜像。
- 独立探针：`web/workbench/src/file-workflow/review-688.test.ts`（node 环境，12 例）与 `review-688-persist.test.ts`（jsdom，2 例），仅用于审查，交付前已删除，未进入提交。
- 变异实验：另开 `/private/tmp/sn688-mut` worktree 与 `/private/tmp/sn688-base`（基线 737 复核），变异只在临时副本内，未 push。

基础门禁（在 `/private/tmp/sn688`）：

```text
$ cd web/workbench && pnpm test
 Test Files  61 passed (61)
      Tests  743 passed (743)

$ cd /private/tmp/sn688-base && cd web/workbench && pnpm test     # 基线 6b6e2dd4
      Tests  737 passed (737)                                      # 作者"基线 737"成立，+6 与 6 个新 it 块一致

$ pnpm typecheck      → web/shared, packages/contracts, web/workbench, web/admin 全 Done
$ pnpm lint           → Design tokens … [PASSED]；ESLint: passed
$ pnpm format:check   → All matched files use Prettier code style!
$ pnpm build          → web/workbench ✓ built（仅既有 500 kB chunk 警告）
```

## 八条逐条判定

### 1. `input.text` 是否真的被 admission 接受 —— 通过

作者说法成立，且我在**契约与后端两侧**独立验证：

1. 用真实契约校验器校验工作台实际发出的 body（由探针从 `FileTaskSession` 真实提交路径抓取，落盘 `/tmp/sn688-workbench-body.json`）：

```text
{"kind":"tts","model":"tts-quality","workspace_id":"ws-default","input":{"text":"哨兵正文-688-SENTINEL-不得入盘"},
 "voice":{"id":"voice-demo","revision":"1"},"profile":{"id":"profile-demo","revision":"2"},
 "output":{"audio_format":"wav","save_artifacts":true}}

CONTRACT_VALIDATION: accepted          # parse("JobCreateRequest", body)，与 jobs.py:457 同一校验器
ADMISSION: text is str = True | chars = 22
```

2. admission 判定镜像（`public_job_admission.py:672-676`：`request_input = intent["input"]` → 含 `text_asset_id` 即 422 → 否则 `text = request_input.get("text")` 并要求 `isinstance(text, str)`）对上述 body 放行；body 里 `input` 的唯一键是 `text`。
3. 旁证：后端自己的集成用例 `tests/integration/test_public_job_atomic_admission_pg.py:206-222` 对 `kind=tts` 的默认 intent 就是 `{"text": "synthetic public job"}` 并断言 `created`；`:349-353` 单独断言 `{"text_asset_id": ...}` 被拒为 `job_text_asset_unsupported`。即「inline text 是后端认定形状」不是我推断的。

`TtsJobInput` 是 `oneOf`（`contracts/speech-openapi.yaml:4164-4183`），`text_asset_id` 分支仍在类型层合法——作者这一点诚实且成立，我的 Python 复核确认 `parse` 接受 `{text_asset_id}`、拒绝越界形状（`200000` 接受 / `200001` 拒绝、`{}`、`text:""`、`text`+`text_asset_id` 并存均拒绝）。422 是 admission 规则不是类型规则。

### 2. 正文真的没进持久化吗 —— 通过（见文末「验证深度」）

结论：**没有进入任何持久化面**。我用四条独立证据链验证（提交后 / 重放后 / 刷新恢复后 / 其它持久化面），详见末节。最强证据是提交全生命周期内**每一次** `setItem` 的原文，4 次写入全部不含正文：

```text
tts -> {…,"pending":"upload",…,"inputFingerprint":"sha256-chain-v1:d1f027d1…","version":4}
tts -> {…,"pending":"validation","assetId":"asset-text","assetBytes":38,"assetSha256":"sha256:aaaa…","version":4}
tts -> {"model":"tts-quality","assetId":"asset-text",…,"pending":"create",…,"version":2,
        "voiceId":"voice-demo","voiceRevision":"1","profileId":"profile-demo","profileRevision":"2"}
tts -> {…同上…,"jobId":"job-demo","pending":null,…,"effectiveModelKey":"[\"tts-quality\",\"1\"]"}
```

### 3. 重放路径的新依赖是否安全 —— 通过，但有三项边界发现

- 路由确实存在且语义匹配：`GET /v1/nexus/assets/{asset_id}/content`，`operationId: downloadAssetContent`，200/206 + `application/octet-stream`（`contracts/speech-openapi.yaml:1290-1338`）；后端 `assets.py:160-219` 原样返回素材字节，`service.download` 只要求 `status == "ready"`（不按 purpose 过滤），因此 `text_input` 素材可读。
- 素材不可读时的行为，我造了 5 类故障（404 deleted / 410 expired / 503 unavailable / 非法 UTF-8 / >4 MiB）：

```text
✓ 内容 404/410/503/非法 UTF-8/超限时：content 读取真实发生（contentCalls=1），
  create 次数 0、createKey 序列长度 0、journal 原文逐字节不变（toBe 同一字符串）、
  snapshot = { unknown: true, canReplay: true }，notice 含「保留原请求身份」。
✓ 素材恢复可读后同一记录重放：keys 长度 2 且 Set 大小为 1，bodies[1] === bodies[0]，executions === 1。
✓ operation_in_progress 后连点两次 replay：creates 仍为 1，journal 不变。
```

即：**不退化新建、不换 key、不写替代任务**成立。`assetId` 进入不可变字段表后，重放也不再可能把 body 拼到另一个素材上（变异实验：从 `fixed` 删除 `assetId` 后作者用例 `rejects changing pinned assetId` 立即红）。

三项边界发现（都不是本 PR 引入的错误，但由本 PR 首次变得可达，需要记录/跟进）：

- **B1 长正文会被 400 顶掉**：`job_receive_max_bytes` 默认 `262144`（`backend/src/speechnexus_backend/settings.py:174`，上限 1 MiB，部署无覆盖），而契约与工作台 `textarea` 都允许 200000 字符。实测 body 空壳 228 字节，故 CJK 上限约 **87305 字符**（4 字节字符约 65479，ASCII 约 261916）。超过即 `_TooLargeError` → `job_error("invalid_job_request")` = **400**。作者未列出此项。
- **B2 重放窗口被素材 TTL 限制**：`asset_default_ttl_seconds = 604800`（7 天，`settings.py:56`），过期由 `Assets.maintain` 真删对象版本。未提交意图若在 7 天内未重放，正文将永久不可读——按当前语义会保持「可重放但永远失败」的死胡同（安全，但无恢复路径；且工作台无「按 createKey 查询原 Job」的能力）。作者未列出此项。
- **B3 4 MiB 上限与 200000 字符自洽（结论）**：自洽且有余量。200000 字符最坏 UTF-8 为 4 字节/码位 = **800000 字节 = 781 KiB**（我实测写入 `/tmp/sn688-worst-bytes.txt`），小于 4 MiB；4 MiB 也恰好等于后端 `asset_max_bytes` 的硬上限（`settings.py:57`），而本客户端上传受 200000 字符约束，实际素材 ≤800 KB，故 4 MiB 永远不会误伤本路径。
- **B4（补充）严格 UTF-8 / BOM 不会把可重建的 body 变成不可重建**：`fatal:true` 只把「非法字节」判为不可读（此时宁可保留原身份也不发错 body，方向正确）；`ignoreBOM:true` 保留 BOM 字符，正好抵消“默认解码器吞掉 BOM”的坑。而工作台侧 `startText`/`textUploadFile` 早已用默认解码器做了 encode/decode 往返校验（`file-task-session.ts:540-541`、`input-fingerprint.ts:12-15`），首字符 U+FEFF 的正文在入口即被拒绝，因此生产路径不会出现「上传时吞 BOM、读回时留 BOM」的不一致。

### 4. `"\u0000"` 占位是否可能被当真发送或真持久化 —— 通过

- `commandShape()` 是模块私有（`task-journal.ts:97`，未导出），只出现在三处：`validRecord` 校验（185）、journal 写入屏障的两处 `JSON.stringify(...)` 比较（253-254、262）。`shapeText`（86）不进入任何 `save`/`JSON.stringify` 结果、不进 notice。
- 唯一发请求的路径是 `#submit` → `#submitCommand` → `jobCommand(record, text)`，其中 `text` 只来自内存正文或 `assetText()` 解码结果；`jobCommand(record)` 缺正文时抛 `TaskJournalError("invalid")`（我直接断言）。
- 变异实测：把 `#submitCommand` 的 catch 改成 `text = "\u0000"` 后，我的探针 **5 例立即红**（断言「create 0 次 + journal 不变」被打破）。同时暴露出作者 33 个用例在该变异下**全绿**——见必须修 M1。

### 5. 未知保护不回归 —— 通过

作者列的三组我逐项独立跑绿（`create-unknown` 重放、`operation_in_progress` 不可重放、cancel 未知），并另写「丢失响应 → 重放」用例，用**严格幂等传输**（同 key 不同 body 直接 409、`executions` 只对新 intent 计数）：

```text
✓ 丢失响应：createKeys 长度 2、Set 大小 1、bodies[1] === bodies[0]、executions === 1，
  最终 { unknown: false, canReplay: false }，journal pending → null。
✓ operation_in_progress：creates === 1，canReplay 保持 false，journal 写入 operationId。
✓ 素材读失败 + operation_in_progress 叠加：连点两次 replay 仍 creates === 1。
```

`#run` 的新分支（`file-task-session.ts:295-305`）只把 `unknown/canReplay` 置为“仍可重放”，没有改动既有 `operation_in_progress`/`response_not_replayable` 的 `#replay = undefined` 与 `canReplay: false`，我核对 diff 确认未放宽。

### 6. 假红 / 真红 —— 通过（数字与红因完全复现）

在临时 worktree `/private/tmp/sn688-mut` 把 `ttsCommand` 的 `input` 回滚为 `{ text_asset_id: record.assetId }`：

```text
 Tests  3 failed | 30 passed (33)        # 与作者"3 failed | 30 passed"一致
FAIL … roundtrips v2 metadata and reconstructs the exact text/voice/profile references
     expected input { text: '合成文本。' } ← received { text_asset_id: 'asset-text' }
FAIL … submits the contract-supported inline text that admission accepts
     expected { text_asset_id: 'asset-text' } to deeply equal { text: '哨兵…' }
FAIL … keeps the sentinel body out of every durable journal record it replays
     expected undefined to be '哨兵-685-SENTINEL-正文不得入库'
```

三处红因全部是「发了 `text_asset_id`」，没有连带。全量套件在该变异下 `6 failed | 751 passed (757)`（= 上述 3 + `TtsTaskHost.test.tsx` 的 `input:{text}` 期望 + 我的 2 个探针），说明回滚不会“绿着混过去”。

### 7. 义务边界 —— 通过

`git show --name-only b8962c42` 只有 7 个文件，全部在 `web/workbench/src/file-workflow/`：`file-task-session.ts`、`task-journal.ts`、`speech-file-client.ts`、`dev/tts-demo-transport.ts` 与 3 个测试文件。**未触碰** `contracts/`、`backend/`、`deploy/`、`node-controller/`、`.github/workflows/`、其他 `web/` 面板（`web/admin`、`web/shared`、其它功能目录零改动）；`package.json`/`pnpm-lock.yaml`/`pyproject.toml`/`uv.lock` 均未改，diff 内无新增 import（用到的 `TextDecoder`、`getReader()` 都是平台 API）。作者 PR 正文的「不改 contracts/backend/deploy/CI，无新依赖」属实。

### 8. 诚实度 —— 基本通过，但有 3 项未列

作者自列的 4 条未验证项经核实**属实且不夸大**：

| 作者声明 | 我的核实 |
| --- | --- |
| 真实后端端到端未跑 | 属实。我把它压缩到「真实契约校验器 + admission 读取逻辑源码镜像 + 后端自己的集成用例默认 intent」三点，仍不等于真实端到端（无 PG/MinIO、无 HTTP 装配） |
| `tests/integration/test_public_job_atomic_admission_pg.py` 未执行 | 属实。CI 该 PR 的 6 个 Integration shard 全部 `SKIPPED`（scope routing：本改动只有 `web/workbench`） |
| 浏览器手工未跑 | 属实，且我也未跑真实浏览器；jsdom 只能证明存储面与逻辑，不证明真实多部分上传与真实路由 |
| CI 结论来自 PR 分支 | 属实。`statusCheckRollup` 对应 head `b8962c42`（与我审查的 SHA 相同），required checks 全 SUCCESS，`Web: lint, types, tests, build and accessibility`、`Python: …`、各 Gate 均 SUCCESS |

作者**未列出**的未验/偏差项（我补三条）：

1. 新增的 `assetText()` 失败分支、4 MiB 上限、严格 UTF-8 行为、`OriginalTextUnavailableError` 语义 **零测试覆盖**（见 M1）——不是“未验证范围”，而是“未知的未验证范围”。
2. issue #685 AC 第 4 条「`parse` 在 `text_asset_id` 下失败」在**类型层不可满足**（契约 `oneOf` 保留该分支）。作者在代码注释与测试里诚实标注了，但 PR 正文只写「契约类型双向」，未点明「这是对 AC 的偏离，需要改 AC」。
3. 上文 B1（长正文 400）与 B2（7 天 TTL 重放窗口）未在 PR 正文出现。

## 必须修清单

### 合并前必须（阻塞）

- **M1 补 `assetText()` 失败路径的回归测试。** 这是本 PR 唯一的新增网络面与最脆弱分支，当前仅 `TtsTaskHost.test.tsx:97` 用 `mockResolvedValue(text)`（永远成功）覆盖。至少三个用例：①内容 404/410/503 时 `create` 次数不变、journal 原文不变、`{unknown:true,canReplay:true}`；②素材恢复可读后重放得到**同一 key、同一 body**；③内容非法 UTF-8 或超 4 MiB 时不外发占位/替代正文。
  证据：我把 catch 改成 `text = "\u0000"` 后作者 33 例全绿，我的探针 5 例红——说明现有测试对「替代正文被发出」零咬合力。
- **M2 在 PR 正文/issue 里书面澄清 issue #685 AC 第 4 条不可满足。** 应写明：`TtsJobInput` 的 `oneOf` 在类型层保留 `text_asset_id`，422 来自 admission 规则；测试因此断言「类型层接受、admission 拒绝」，建议同步修正 #685 的 AC 措辞，避免后续复核把这条判成假红。

### 合并前必须记录并开工跟进（不阻塞本 PR 合并）

- **M3 长正文 400 边界。** 契约 200000 字符 vs 服务端 `job_receive_max_bytes=262144` 字节（CJK 约 8.7 万字符即触顶，`invalid_job_request`/400）。本 PR 不得改后端/部署，但需开 follow-up（提高 `job_receive_max_bytes` 至 ≥640 KiB，或契约/前端下调上限）并在 PR 正文“影响面/已知限制”中写明。
- **M4 重放窗口文档化。** 未知意图的重放依赖文本素材，而素材 TTL 默认 7 天且过期会真删对象；应写进恢复语义文档/PR 正文，说明「超过素材 TTL 的未提交意图不可再重放，且按设计绝不创建替代任务」。

### 建议（非必须）

- `#command` 已成死字段（`file-task-session.ts:84/511/514`：赋值后下一行即用，旧的「重放缓存 payload」用途已消失）。可改为局部变量，同时避免正文在会话生命周期内被类字段长期引用；`dispose()` 也未清理它。
- `assetText()` 未与素材声明长度/身份交叉校验（对比 `download()` 的 `bytes !== artifact.bytes` 检查）：截断或内容被替换的响应会得到不同 body，同 key 触发 409（安全，不新建）但会耗尽原意图。可在读取后与 `getAsset(assetId).bytes` 比对，或把 `assetBytes` 一起纳入不可变记录。
- `TtsTaskHost.test.tsx` 对 `assetText` 的 stub 建议加注释说明“成功路径假设”，避免读者误以为覆盖了失败分支。

## 假红清单

| 现象 | 判定 |
| --- | --- |
| 回滚 wire 形状后 `3 failed \| 30 passed` | **真红**，红因均为「发了 `text_asset_id`」，我独立复现数字与 red 原因一致 |
| `parse("JobCreateRequest", {text_asset_id})` 不抛错 | **不是假红**，也不是缺陷：契约类型层确实保留该分支（我用后端 Python 校验器复核）。issue #685 AC 第 4 条按字面不可满足，需改 AC（M2） |
| `TtsTaskHost.test.tsx` 中 `assetText` 被 mock 成恒成功 | **覆盖面假象**：该文件看起来“测了重放”，实际只覆盖内容可读路径；不能当作新分支已验证（见 M1） |
| `pnpm test` 抓不到「读不到素材却发占位正文」 | **假绿**：变异实测作者套件全绿，新分支当前无咬合力（M1） |
| CI 绿灯 | 覆盖范围内为真（Web/Python/Gate 全绿色对 head `b8962c42`），但 Integration shard 全 SKIPPED，不能作为 admission 端到端证据 |

## 我独立复现的反例（六组，全部用自写探针，未改产品代码）

| # | 反例 | 结果 |
| --- | --- | --- |
| 1 | 交叉语言校验：真实 Python 契约校验器 + admission 读取镜像 + 200000/200001 边界 | 接受；`text_asset_id` 类型层接受、admission 拒绝（见 §1 输出） |
| 2 | 持久化面扫描：4 次 `setItem` 原文、全部 snapshot/notice、jsdom 真实 `sessionStorage`/`localStorage`/`document.cookie`、base64 形态、`\u0000` 形态 | 全部不含正文（见「验证深度」） |
| 3 | 依赖故障矩阵 5 类（404/410/503/非法 UTF-8/>4 MiB）+ 恢复后重放 | 0 次 create、journal 逐字节不变、同 key 同 body、`executions===1` |
| 4 | 占位/替代正文外发变异（`catch → "\u0000"`） | 我的 5 例红；作者套件全绿（暴露 M1） |
| 5 | 未知保护：丢失响应、`operation_in_progress`、两者与素材读失败叠加 | 同 key 同 body、`executions===1`、不可重放态不被放宽 |
| 6 | wire 形状回滚变异（临时 worktree） | `3 failed \| 30 passed`，红因正确 |

附加变异（用于验证「作者对自身改动的保护是否到位」）：从 `fixed` 删除 `assetId` → 作者用例 `rejects changing pinned assetId` 1 红，说明该不可变字段确实被测试钉住。

## 「正文不持久化」这条性质我验到什么程度

这是本卡核心，我按「声明 → 实际写入 → 其它持久化面 → 反证」四层验到可复述的程度：

1. **写入原文级**：包装 `setItem` 记录**每一次**写入的完整字符串。一次完整「提交 → 丢失响应 → 刷新恢复 → 重放」生命周期共 4 次写入（v4 upload、v4 validation、v2 create、v2 confirmed），逐条断言不含哨兵正文、不含 base64 形态、不含 `\u0000`/`\\u0000`（占位符未落盘）。
2. **状态面**：订阅 session 的每一次快照（含全部 notice 文案）并扫描；`JSON.stringify(getSnapshot())` 不含正文。notice 均为常量或 `SpeechResponseError.message`（只含 status+code），无正文插值（逐条核对 `file-task-session.ts` 20 处 notice）。
3. **真实浏览器存储**：jsdom 环境下用真实 `window.sessionStorage` 走「恢复未提交意图 → 重放 → 再刷新」与「提交 → 刷新恢复」两条路径，把 `sessionStorage`/`localStorage`/`document.cookie` 的**全部键值**拼起来扫描哨兵、base64、`\u0000`——全空。静态核对：`web/workbench/src` 内 `localStorage` 仅 `teleprompter` 使用（与 TTS 正文无关）、**无任何 IndexedDB 使用**、`file-workflow/` 内无 `console.*`；`createKey`/`cancelKey`/`uploadKey` 均为 `crypto.randomUUID()`，键名不派生自正文。
4. **反证（负向验证）**：把「素材读不到」实现成 fallback 占位正文的变异后，我的持久化/外发断言立即变红——说明上面的绿不是“测不到”，而是“确实没发生”。同时确认 `#text` 在提交前被清空、`jobCommand(record)` 缺正文直接抛错，故正文只有「提交时内存」与「素材内」两个出处。

**边界与诚实的缺口（我必须说明）**：

- 我**没有**做真实浏览器 + 真实后端（PG/MinIO/对象存储）的端到端，也未抓真实 HTTP 报文；结论成立于「契约校验器真实、admission 逻辑为源码逐行镜像、存储面在 jsdom 真实实现」的范围内。
- 持久化记录里确实存在**正文的派生量**，不是正文本身：`inputFingerprint`（`sha256-chain-v1:…`，v4/v6 上传记录，用于“同一输入才允许恢复”）、`assetBytes`（正文 UTF-8 字节数，如 38）、`assetSha256`（素材内容摘要）。因此严格表述是「正文不在本地持久化；但正文的哈希、字节长度与素材摘要会持久化」。这属于本 PR 之前就存在的设计，不是本次引入；若威胁模型包含低熵正文的字典比对，需要单独评估。
- 刷新发生在**上传尚未确认**（v4/v6 `pending:"upload"`）时，正文本来就已丢，需要用户重新输入并用 fingerprint 校验（`resumeText`）——这与持久化规则一致，也是本 PR 未改变的行为；重放从素材读回正文只在「上传已确认、assetId 已落盘」后成立。

## 只能依赖作者陈述（我无法独立验证）

1. 作者本地跑过的 `pnpm test:a11y`（52 passed）与根 `pnpm test`（admin 151 / contracts）——我只跑了 workbench 套件、全量 typecheck/lint/format:check 与 `pnpm build`；a11y 需要 playwright 浏览器，CI 的 Web 作业已成功（间接佐证）。
2. 作者在真实浏览器里的手工点击（本人明确声明未跑，故不算依赖）。
3. 真实部署下 `job_receive_max_bytes` 的生效值：我只能读到默认 `262144` 并确认仓库内无覆盖（`deploy/`、`docs/`、`.github/` 均无该变量），实际部署环境若有覆盖，B1 的阈值随之变化。
4. 素材 7 天 TTL 的实际维护频率（`Assets.maintain` 由谁、多久跑一次），我只读了代码与默认配置。

## 时间线

| 时间（本地 +08:00） | 动作 |
| --- | --- |
| 02:05 | 确认主检出停在 `docs/tracker-batch12`（`uv.lock` 有未提交改动，未触碰）；`git fetch`；核实 head `b8962c42` 与基线 `6b6e2dd4` |
| 02:06 | 建独立 worktree `/private/tmp/sn688`；离线 `pnpm install`（lockfile 未变） |
| 02:08 | `pnpm test` → 61 文件 / 743 passed；之后 typecheck / lint / format:check 全绿 |
| 02:09-02:11 | 读 head 的 `file-task-session.ts` / `task-journal.ts` / `speech-file-client.ts` / 契约 `TtsJobInput` / `public_job_admission.py` / 素材内容路由 |
| 02:11-02:16 | 写并跑 12+2 个独立探针（wire 形状、持久化面、依赖故障矩阵、未知保护、占位符外发） |
| 02:12 | 交叉语言校验真实 body（Python 契约校验器 + admission 镜像） |
| 02:16 | 基线 worktree 复核 737 passed |
| 02:17 | 变异 worktree：wire 形状回滚 → `3 failed \| 30 passed`（与作者一致） |
| 02:18 | 追加变异：占位符 fallback（作者套件全绿，探针 5 红）、`fixed` 去掉 `assetId`（作者用例 1 红） |
| 02:19 | 核查边界：`job_receive_max_bytes` / `asset_max_bytes` / 素材 TTL / 长正文 400 阈值；CI `statusCheckRollup` 与 PR 正文；确认 integration shard 全 SKIPPED |
| 02:2x | 写本报告；删除临时探针；复跑作者套件确认原样 743 绿；`git checkout -- uv.lock` 后提交并开 PR |
