# 工作台 TTS 修复的探针同步增量审查（PR #688 / `e58e16e7`，追加 head `47e19feb`）

> 过程记录文档：保留时间线、独立复现的变异与命令输出。结论面向「本次增量是否守住授权条件」，
> 不修改产品代码与探针。文件名沿用任务指定的 `2026-11-13`；实际审查发生在 2026-11-14 凌晨，
> 下文时间线用本地时间（+08:00）。

## 结论

**有条件通过**。四条授权条件全部守住，M1 的 8 例回滚必红经我独立复现（数字、逐例红因、外发正文
均与作者一致）。不通过的理由不存在；但**记录/声明层有四项必须更正**（其中一项是探针报告字段与
探针真实行为不符），且这些都不是「断言被削弱」——探针仍有牙，第 5 条要求的「发错误正文」变异
让两个探针同时变红。

已合并事实与本次核验范围：`e58e16e7`（PR #688 squash）的树与追加 head `47e19feb` 的树**完全相同**
（`9a448a325ea5382237d430cc9e96c04650a748c3`），`47e19feb` 只改两个探针（`+25/−5`），本报告核的
就是这 25 行与 M1 的 105 行测试补充。

## 审查对象与方法

- 独立 worktree：`git worktree add /private/tmp/sn692 -b review/692-tts-delta origin/main`（主检出停在
  `docs/tracker-batch12`，`uv.lock` 有未提交改动，全程未触碰；所有内容在 worktree 内读）。
- 变异 worktree：`/private/tmp/sn692-mut`（`e58e16e7`），变异只存在于该临时副本，未提交、未 push。
- 环境：node v26.12.1、pnpm 12.14.0、Playwright Chromium 156.0.8078.4、ruff 0.17.2（与 CI 钉住版本一致）。
- 依赖：`pnpm install --frozen-lockfile --prefer-offline`（离线复用 store，`pnpm-lock.yaml` 与
  `uv.lock` 均未改动）。
- 本地实跑：两个探针在本 worktree 直接 `node` 运行（与
  `tests/integration/test_workbench_orphan_probes.py::run_probe` 同一入口）、`tts-task.test.ts`、
  `web/workbench` 全量套件。
- CI 证据：PR head 的 full 运行 `43487621255`（pick 出的是 `refs/pull/688/merge` =
  `0dc51d3 Merge 47e19feb into d4d56cf2`）与合并后 main 的 full 运行 `43490311138`（`source_head`
  `e58e16e7`），从 shard 4/5 的 artifact 与 job log 取 nodeid、outcome、`--durations=20` 的 call 耗时。

## 七条逐条判定

### 1. 不削弱探针（`workbench-tts-host-probe.mjs`）—— 守住

| 要求 | 证据 |
| --- | --- |
| `body.input` 仍是精确相等 | `scripts/dev-env/workbench-tts-host-probe.mjs:172` 为 `assert.deepEqual(body.input, { text })`；`node:assert/strict` 即 `deepStrictEqual`。两探针全文 `grep 'hasOwnProperty\|toMatchObject'` 为空 |
| mock 真加了 content 路由 | `:186-192` `GET /v1/nexus/assets/asset-text/content` 返回 `payload`（上传路由 `:150` 已断言上传字节 `=== payload`）。该路由**在关键路径上**：我把它改成返回 `wrong readback` 后探针 exit 1，断言差异为 `+ text: 'wrong readback'` vs `- text: '合成测试，不是真实业务文本。'` |
| 「可点 → 点击 → 同 key 同 body」仍被断言 | `:308` `await expect(button("按原键重放")).toBeEnabled()` → `:309` reload → `:310` 点击 → `:312-314` `creates.length === 2`、`deepEqual(creates[0], creates[1])`、`creates[0].key === original.createKey`。反向证据：把探针换回 `d4d56cf2` 版本（无 content 路由）跑新代码，exit 1，失败即 `locator resolved to <button disabled>按原键重放</button> / Timeout 30000ms` |

### 2. reconcile 探针不许跳过 TTS —— 守住（但报告字段失真，见 5）

- `workbench-asset-reconcile-probe.mjs:116-119` 按 `kind` 分支：`asr` 断言 `input.audio_asset_id`，
  `tts` 断言 `assert.deepEqual(command.input, { text: "synthetic original text" })`（不让形状带 id）。
- 10 个 scenario（asr/tts × 5）全部执行，TTS 未被 skip：本地运行结果里 `tts/hint` 为
  `uploads 1 / jobs 1`，与作者陈述一致；`tests/integration/…orphan_probes.py:415` 的
  `len(payload["scenarios"]) == 10` 仍在（若把 TTS 整体 skip 该断言立刻红）。
- TTS 分支确实被**真实驱动**到：我在临时副本给 content 路由加打印，运行后只有一次
  `REVIEW-692 CONTENT-GET {"kind":"tts"}` —— 即 TTS 的重建 body 依赖该路由，断言不是空转。

### 3. 探针真被跑到 —— 守住（两次 full 运行、nodeid 与耗时齐全）

PR head（`47e19feb` 合入 `d4d56cf2` 的 pick 运行 `43487621255`，mode=`full`，6 个 shard 全 success）：

| shard | 用例 | outcome | call 耗时（`--durations=20`） |
| --- | --- | --- | --- |
| Integration 4 | `tests/integration/test_workbench_orphan_probes.py::test_the_asset_reconcile_probe_stays_on_synthetic_loopback` | passed | 5.78s |
| Integration 5 | `tests/integration/test_workbench_orphan_probes.py::test_the_tts_host_probe_keeps_late_audio_and_url_revocation_honest` | passed | 3.99s |

合并后 main 的 full 运行 `43490311138`（`source_head=e58e16e7`，6 shard 全 success）：

| shard | 用例 | outcome | call 耗时 |
| --- | --- | --- | --- |
| Integration 4 | `…::test_the_asset_reconcile_probe_stays_on_synthetic_loopback` | passed | 4.02s |
| Integration 5 | `…::test_the_tts_host_probe_keeps_late_audio_and_url_revocation_honest` | passed | 6.16s |

两条证据都来自 shard artifact 的 `selected`/`outcomes`（不是 `skipped`），且 pytest 的慢用例表有
非零 call 耗时，排除「空断言/跳过」。本地重跑亦 exit 0（`creates: 3`、`handler_errors: []`；10 scenario）。

### 4. PR 正文 —— 写入到位，但最终状态结论过期（必须更正）

- 「探针是 body 形状的消费者、本卡同步了它们」：已写入，PR #688 正文 §「探针消费者同步（审查后追加，
  lead 授权）」明确写了 `JobCreateRequest` 的 body 形状是本卡改的对象、两个 dev 探针是消费者，并列出
  两个探针各自改了什么（含「仍是 `deepEqual` 精确相等」）。**满足**。
- 「分支落后 main 时两点 diff 把 main 的 `.github` 提交算成本分支删除 → 误跑 full」：已写入正文
  §「CI 路由的既有行为（非本卡引入，记录）」，与 `scripts/dev-env/ci-routing.py` 的实现一致
  （`_git diff --name-only … base HEAD` 两点 diff）。**满足**。
- 但同一段的具体结论不成立：正文写「rebase 到 `d4d56cf2`，routing 立即回到 `web`，integration shard
  正确 `skipping`」。这只对**探针同步之前**的两个 head 成立（我核对 routing 作业输出：`b8962c42`、
  `5a25e282` 均 `{"mode":"web"…}`）；追加探针后 `scripts/dev-env/*.mjs` 不属于任何已知分类，
  `classify()` 直接 `full`，最终 head 的 routing 输出是
  `{"mode":"full","reason":"Shared, unknown, executable or empty changes require every verification lane."}`，
  Integration 0–5 全部执行。这是**记录错误**，不是行为缺陷（反而让两个探针在 PR CI 里就被跑到）。

### 5. 有没有新绿 —— 断言没松，但 reconcile 探针的报告字段失真

变异证明探针仍有牙（详见下节）：把工作台改成发 `{ text: "wrong text from workbench" }` 后，两个探针
同时 exit 1，红因正是 body 形状断言：

```text
REVIEW-692 HANDLER: Expected values to be strictly deep-equal:
+ actual - expected

  {
+   text: 'wrong text from workbench'
-   text: '合成测试，不是真实业务文本。'
  }
```

```text
REVIEW-692 ASSETHANDLER: Expected values to be strictly deep-equal:
+   text: 'wrong text from workbench'
-   text: 'synthetic original text'
```

**新发现（本次增量引入的报告失真）**：`workbench-asset-reconcile-probe.mjs` 现在会真实
`GET /v1/nexus/assets/asset-original/content`（我加了打印，仅 `tts/hint` 一次），但报告仍硬编码
`metadataOnly: true`（`:340`），`tests/integration/…orphan_probes.py:419` 也照旧断言它为真。
该字段在改动前的含义是「只读素材元数据、不取内容」——改动后不再成立，且报告里没有 content 读计数
可让读者看出这一点。断言本身没有被削弱，但**证据声明与行为不符**，属于本仓库一贯在意的
「探针自我声明必须诚实」。

### 6. `e58e16e7` 与 `47e19feb` 的差异 —— 只有两个探针，无顺手改动

- `git rev-parse 47e19feb^{tree} e58e16e7^{tree}` 两个都是 `9a448a32…`（树相同；差别只是 squash）。
- `git show --name-status 47e19feb`：只有 `scripts/dev-env/workbench-tts-host-probe.mjs` 与
  `scripts/dev-env/workbench-asset-reconcile-probe.mjs`（`+25/−5`）。
- 整个 PR 的提交面 `git diff --stat d4d56cf2 e58e16e7`：9 个文件、`+461/−62` = 7 个
  `web/workbench/src/file-workflow/` 文件 + 这 2 个探针；`contracts/`、`backend/`、`deploy/`、
  `.github/`、`package.json`、`pnpm-lock.yaml`、`uv.lock` 零改动。审查后追加的 M1 改动也只落在
  `tts-task.test.ts`（+105）、`file-task-session.ts`（8 行）、`TtsTaskHost.test.tsx`（1 行），
  没有牵连别处。

### 7. 诚实度 —— 基本完整，三处需要补正

作者列出的未验证项经核实**属实**：

| 作者声明 | 核实 |
| --- | --- |
| 真实后端端到端未跑 | 属实：两个探针都是合成 loopback HTTP + 真实 Chromium，`real_backend_or_model_qualification: false` / `productionUI: false` 都写在报告里 |
| 探针是合成 HTTP + 真实 Chromium | 属实：本地实跑 Chromium 156，`synthetic_loopback: true` |
| #690 边界（200000 字符 vs 262144 字节） | 属实：#690 已开（OPEN，2026-11-13T18:30Z），正文与 PR 描述一致 |
| 素材 TTL 与重放窗口 | 属实：写在 PR 正文 M4 |
| M2（AC 第 4 条类型层不可满足） | 已按上一轮要求写入 PR 正文，**前一轮 M2 收口** |

需要补正的三处：

1. 上述 `metadataOnly: true` 与真实行为不符（第 5 条）。
2. PR 正文「routing 回到 web / shard skipping」对最终 head 不成立（第 4 条）。
3. 正文「探针抓到变异后被真正外发的替代正文」把功劳记在两个 dev 探针上，不准确：我把占位 fallback
   变异打进新代码后，两个 dev 探针**仍然 exit 0**（合成 content 路由永远成功，`assetText()` 不会抛），
   抓到 `{"input":{"text":"\u0000"}}` 的是 M1 用例里那个 `creates` 捕获，不是这两个探针。
   另：`file-task-session.ts:328` 新增注释称「terminal contract rejection（4xx）」，但代码没有状态码
   判断（`#691` 记录的既有行为），注释与实现对不上，建议改注释或引用 #691。

## M1 独立复现（占位 fallback 回滚）

在 `/private/tmp/sn692-mut` 把 `#submitCommand` 的 catch 改成 `text = "\u0000"`（与作者描述的变异逐字
相同，只改这一处）：

```text
 Tests  8 failed | 33 passed (41)
```

- **8 例全红，且是各自的断言红、不是连带**：5 个故障类用例红在 `tts-task.test.ts:528`
  （`expect(guard.creates).toHaveLength(1)`），恢复重放 1 例红在 `:541`（同一条 create 计数断言），
  非法 UTF-8/超限 2 例红在 `:561`。其余 33 例（含既有 27+6 例）全绿。
- **外发正文被真正抓到**（在临时副本给 503 恢复用例加一行 `console.log`）：

```text
REVIEW-692-OUTBOUND {"kind":"tts","model":"tts-quality","workspace_id":"ws-default",
 "input":{"text":"\u0000"},"voice":{"id":"voice-demo","revision":"1"},
 "profile":{"id":"profile-demo","revision":"2"},"output":{"audio_format":"wav","save_artifacts":true}}
```

- **断言覆盖面（读码逐条核对）**：故障类断言 `creates` 次数不变 + `test.data.get("tts")` 用 `toBe`
  比对同一字符串（journal 逐字节不变）+ `{unknown:true, canReplay:true}` + `job` 为空 +
  notice 含「原文本素材暂不可读取」；恢复重放断言 `creates[1] === originalBody`、
  `Set(createKeys).size === 1`、`bodies[1] === bodies[0]`、`executions === 1`、`pending === null`；
  非法 UTF-8/超限两例还断言恢复后 body 的 `input.text === sentinel`、`originalBody` 不含 `\u0000`。
- **没有「截断内容兜底」**：实现上 `assetText()` 在 `bytes > 4 MiB` 时先抛（`speech-file-client.ts:141`），
  再用 `TextDecoder("utf-8", { fatal: true, ignoreBOM: true })` 解码（`:150`），不会产生占位/截断正文。
  我另做两个针对性变异验证这两道闸有咬合力：
  - `fatal: false` → 2 failed（5 类中的 `invalid-utf8` 与 `never fabricates … invalid-utf8`）；
  - 删掉 4 MiB 上限 → 1 failed（`oversize` 类在 notice 断言上红；此处没有外发替代正文，因为
    `JobCreateRequest.text.maxLength` 会在客户端把它挡掉）。

## 我独立复现的变异（全部在临时副本，未 push）

| # | 变异 | 结果 |
| --- | --- | --- |
| A | `#submitCommand` catch → `text = "\u0000"`（作者声称的回滚） | `8 failed \| 33 passed`；逐例红在 528/541/561；捕获外发 `{"input":{"text":"\u0000"}}` |
| A2 | `TextDecoder({fatal:false})` | `2 failed`（非法 UTF-8 两类） |
| A3 | 删除 4 MiB 读取上限 | `1 failed`（超限类的 notice 断言；无替代正文外发） |
| B1 | 工作台 `ttsCommand` 发 `input: { text: "wrong text from workbench" }` | tts-host **exit 1**（`+ text:'wrong text from workbench'` vs 期望常量）；reconcile **exit 1**（同差异，`jobs` 因此为空） |
| B2 | 把 tts-host 的 content 路由改成返回 `wrong readback` | **exit 1**：`+ text:'wrong readback'` vs 期望常量 —— 证明该路由在关键路径上、且读回内容错时断言会红 |
| C | 把两个探针换回 `d4d56cf2` 版本 + 新代码 | tts-host exit 1（重放按钮 disabled 超时）；reconcile exit 1（`actual: []` vs `[createKey]`），与作者「修前红」表一致 |
| D | 占位 fallback 变异 + **两个新探针** | **两个探针都 exit 0** —— 这两个探针不能发现该回归；M1 用例是唯一防线 |

基线复核：clean `e58e16e7` 下 `tts-task.test.ts` 41 passed、`web/workbench` 全量 **751 passed**，
与作者「737 → 751」一致。

## 必须修清单

按「已合并」处理为跟进项；本次增量没有阻塞级缺陷。

- **F1（探针报告诚实度，必须）** `workbench-asset-reconcile-probe.mjs` 既然会读素材内容，就不能再硬编码
  `metadataOnly: true`。改成 `metadataOnly: false` 并补 `contentReads` 计数（或删除该字段），同步
  `tests/integration/test_workbench_orphan_probes.py:419` 的断言。
- **F2（记录更正，必须）** PR #688 正文/合并提交说「routing 回到 web、integration shard skipping」，
  与最终 head 的 `mode=full` 不符。以本报告或 PR 评论作为更正记录，避免后续把「探针在 PR CI 未被跑到」
  当成事实。
- **F3（记录精度，必须）** 「探针抓到变异后外发的替代正文」应改为「M1 用例的 create 捕获」；两个 dev
  探针在该变异下仍绿（我实测 D 组），不能算作该回归的防线。
- **F4（注释，建议同步修）** `file-task-session.ts:328` 新增注释称 4xx 终态，但分支对任意
  `SpeechResponseError` 生效（#691 已记录）；注释改为现状描述或直接引用 #691，避免「注释说 A、代码做 B」。
- **F5（可选强化）** reconcile 探针的 TTS 分支目前只靠 `deepEqual(command.input, {text})`；可再加
  一条「TTS 场景 jobs 计数为 1」的显式断言（现状由 `state.jobs` 与报告字段间接保证，已足够，非必须）。

## 只能依赖作者陈述（我无法独立验证）

1. 作者本地跑过的 `pnpm test:a11y`（52 passed）与根 `pnpm test`（admin/contracts 部分）：我只跑了
   `web/workbench`（751 passed）与两个探针；a11y 的表现只能由 CI 的 Web 作业间接佐证。
2. 这两个探针在真实部署（真实后端 + 对象存储）下的行为：本报告的探针证据全部是合成 loopback HTTP +
   真实 Chromium。
3. PR #688 正文中「修前 shard 4/5 红」的原始日志：我只能复现「旧探针 + 新代码红」与「新探针 + 新代码绿」，
   无法回放作者当时本地运行的终端输出。
4. `#690`/`#691` 的实际排期与修复范围：我只核对了 issue 内容与创建时间，未参与其决策。

## 时间线

| 时间（本地 +08:00） | 动作 |
| --- | --- |
| 03:16 | 确认主检出 `docs/tracker-batch12`、`uv.lock` 未提交；`git fetch`；建 `/private/tmp/sn692`；核实 `47e19feb` 树 = `e58e16e7` 树 |
| 03:17 | 离线 `pnpm install`；读两个探针全文、`file-task-session.ts`、`speech-file-client.ts`、M1 8 例 |
| 03:19 | 本地跑 `tts-task.test.ts`（41 passed）与全量 workbench（751 passed） |
| 03:20 | 两个探针本地实跑 exit 0；读 shard 报告结构与 CI 路由规则 |
| 03:20–03:23 | 变异 worktree：A/A2/A3 逐项复现（8/2/1 红） |
| 03:23 | PR head full 运行 `43487621255` 完成；取 shard 4/5 artifact 与 `--durations` |
| 03:24 | 合并后 main full 运行 `43490311138` 完成；取 shard 4/5 证据；复现「修前红」（C） |
| 03:25 | 变异 B1/B2（发错误正文、读回内容错）→ 探针红；核对 PR 正文 routing 结论与 `ci-routing.py` |
| 03:26 | 核对 `metadataOnly` 失真（content GET 打印）、#690/#691 与 #685 交付记录 |
| 03:27–03:30 | 变异 D（占位 fallback + 新探针仍绿）；复核基线；写本报告；`ruff format` 校验；提交并开 PR |
