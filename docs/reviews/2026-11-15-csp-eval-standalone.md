# 非作者审查：部署 CSP 下的契约校验器 —— ajv standalone 预编译（PR #842 / #840）

- 被审提交：`7ce24bba60ea2ba564156c83c8ec1b0cae66a25b`（`fix/840-csp-eval`），标题 `fix(web): 生产构建不再依赖 unsafe-eval 并使部署镜像的浏览器门禁可见 (#840)`
- 基线：`origin/main` = `f7ee17ae168f5521ea15c23761e4cd3de19c3138`
- 审查工作区：`/private/tmp/sn847`（`git worktree add -b review/847-csp-eval origin/main`）；被审内容一律用 `git show 7ce24bba:<path>` 读取，另在 `/private/tmp/sn847-head`（`git archive` 展开的 PR head 快照）与 `/private/tmp/sn847-prhead`（`7ce24bba` 的 detached worktree）中构建与执行
- 审查时间：2026-11-15 17:20–17:45（Asia/Shanghai）
- 审查性质：只读审查；产品代码、测试、脚本、生成物均未修改。所有变异只发生在 `/private/tmp` 下的临时副本，未 push 产品分支
- 前置阅读：`web/AGENTS.md`、`contracts/AGENTS.md`、`.agents/skills/speechnexus-contract-change/SKILL.md`、`docs/validation/ci-validation.md`

## 一、结论

**修复方向正确且核心性质成立，可合入，但附三条必须修。**

`(B) 生成期 ajv standalone 预编译` 确实消灭了部署镜像里唯一的运行期动态求值路径：修前两个入口在真实 Chromium + `nginx.conf` 真实 CSP 下正文为 0、抛 `EvalError`；修后正文 1208 / 752、一级标题可见、0 pageerror、0 console error，且浏览器里实测应用侧 `Function`/`eval` 调用次数为 0。门禁的**双重保护**（CSP 不兼容 → 红；给 CSP 加 `'unsafe-eval'` → 拒绝认证）两条都独立复现成立，且我额外证明了第二条不是摆设：**同一份修前 `dist`，只要把 `'unsafe-eval'` 加进策略就能渲染出 1208 字正文**——这正是被门禁挡住的那条"用放宽策略换绿"的捷径。

真正的欠账不在安全性，而在两处**可验证性**与**一处事实表述**：

1. `format` 不降级这件事**没有任何已提交的测试守护**。作者的自报负例是手工实验（我复现成功），但仓库里搜不到 `2026-13-45` 或任何非法 `date-time` 断言；而"未来 schema 用了未注册 format"会**静默通过**（详见 B4）。
2. 体积回归 (+71% / +62% gz，首屏单文件 2.5 MB / 3.3 MB 原始) 是**真实且未被分摊**的代价，门禁也不测首屏解析成本；PR 里没有把它写成跟踪项。
3. 自报的"两个 bundle 里 `new Function` 0 次"与"ajv 已从产物移除"措辞不准：修前产物里是 `Function(` 构造（不是 `new Function`），修后 ajv 的 runtime helper 与 ajv-formats 的 format 表**仍在产物内**（必须如此），README 的 "build-time inputs" 也因此不准确。

## 二、核心四问

### 1. eval 是否真的消灭了？——是，且是"不再求值"，不是"没被走到"

三条独立证据，其中第一条是对照实验，专门排除"假绿"：

**(a) 浏览器内插桩的对照实验。** 在任何应用脚本之前包住 `window.Function` 与 `window.eval` 计数（`addInitScript`），同一脚本跑修前/修后：

| 入口 | `body.innerText` 长度 | `h1` | 应用侧 `Function(...)` 调用 | pageerror |
| --- | --- | --- | --- | --- |
| 修前 admin | **0** | `[]` | **1**（ajv 编译，抛 EvalError） | `EvalError: Evaluating a string as JavaScript violates … 'unsafe-eval' is not an allowed source` |
| 修前 workbench | **0** | `[]` | **1** | 同上 |
| 修后 admin | **1208** | `["运行事实管理"]` | **0** | 0 |
| 修后 workbench | **752** | `["语音任务集中完成"]` | **0** | 0 |

修前那 1 次调用正是 `packages/contracts/src/profile-runtime.mjs` 里 `ajv.compile()` 的构造点（`Function(`${r.default.self}`, …)`，minify 后不带 `new`）。插桩能观测到它，说明"修后 0 次"不是插桩失灵，而是那条路径真的不存在了。

**(b) 产物静态核验（带阴性对照）。** 同一组 pattern 打在修前/修后 `dist/assets/*.js` 上：

```
--- BEFORE admin          --- AFTER admin
  new Function => 0         new Function => 0
  Function(   => 1          Function(   => 0
  eval(       => 0          eval(       => 0
  setTimeout(" => 0         setTimeout(" => 0
  setInterval(" => 0        setInterval(" => 0
```
workbench 同形（`Function(` 1 → 0）。`grep` 的阴性对照成立：修前命中、修后不命中，不是两侧都 0 的空断言。

**(c) 生成物没有"惰性字符串"。** 全部 28 个 `*.validators.generated.mjs` 里 `require(` 出现 **0 次**（生成器已把 `require("ajv/…")` 改写为 ESM import）；只保留 3 个 specifier：`ajv/dist/runtime/ucs2length.js`、`ajv/dist/runtime/equal.js`、`ajv-formats/dist/formats.js`。作者说"两个 helper"，实际是三个（少了 `ajv-formats/dist/formats.js`，而它正是 format 表的来源）。这些是被 import 的模块对象/函数，不是被求值的字符串。

**(d) 校验器真的在执行（不是死代码）。** 用**同一个 Vite 8 / rolldown** 打包 PR head 的 `profile-runtime` + 两个生成物，在 `script-src 'self'` 下用真实 Chromium 调用：

```
profileGood (2026-11-15T00:00:00Z) => accepted
profileBad  (2026-13-45T99:99:99Z) => rejected: ContractValidationError
profileRawBad                       => false
httpGood / httpBad                  => accepted / rejected: ContractValidationError
profileEntrypoints = 55, httpEntrypoints = 500
Function 调用 = [], eval 调用 = [], CSP violation = [], pageErrors = []
```
若预编译函数是死的（恒真），`profileRawBad` 会是 `true`。它是 `false`，且与 B4 的 format 机制互为印证。

**结论：无第二条 eval 路径。** 全库 grep（`web/*/src`、`packages/contracts/src`）中除 `profile-runtime.mjs` 的注释文字外，没有 `new Function` / `eval(` / `Function(` / 字符串 `setTimeout`；web 源码里也没有 `new RegExp(变量)` 或 `.compile(`。生成物里的 `new RegExp("literal","u")` 属于模块初始化期的正则编译，不受 CSP 限制，也不是求值。

### 2. format 会不会未来静默降级？——今天不降级；未来**会静默**，且现有门禁不会发现

机制层面逐条说清（全部实测，见 B4 详证）：

- `addFormats(ajv)` 在生成器里是**无条件**调用（`generate-standalone-validators.mjs` 第 61 行），runtime 与生成期用的是同一组 Ajv 选项（`strict:false, coerceTypes:false, useDefaults:false, removeAdditional:false, validateFormats:true`），所以**今天的行为与修前等价**：非法 `date-time` 被拒，去掉 `addFormats` 同输入被放行。
- 当前 bundle 用到的 format 只有 `date-time`(262)、`ipv4`(1)、`ipv6`(1)、`binary`(5)，全部由 ajv-formats 注册（其 `fullFormats` 含 27 个）。所以**现状没有未注册 format**。
- 但若将来 schema 写了未注册的 format：`ajv` 在 `strict:false` 下**不报错、退出码 0**，只在 stderr 打两条 `unknown format "…" ignored in schema at path …`，生成的校验器**对该字段完全不校验**（实测 `validators.Thing({at:'not-a-date-at-all'}) === true`）。而 `generate-contract-bindings.py` 用 `subprocess.run(..., capture_output=True)` 调生成器，**这两条警告在日常 `make contracts-generate` 里看不见**。也就是说：这个降级既不体现在 schema diff 里，也不体现在 drift/CI 输出里。
- 修前修后行为一致，所以这不是本 PR 引入的回归；但它把"运行期编译"换成"生成期一次性编译"，意味着**同一个静默降级从"每次运行都可能发现"变成"永不再有机会发现"**——这正是必须用测试把它钉死的原因（见必须修 1）。

### 3. 门禁双重保护是否都成立？——都成立，且我用两条独立反例证明

| 保护 | 构造 | 结果 |
| --- | --- | --- |
| ① CSP 与 bundle 不兼容 → 红 | 把**修前** `dist` 换进门禁的 `web/*/dist`，策略不动 | `2 failed, 5 passed`，退出码 1，报 `admin rendered no body text under the deployed policy` / 同形 workbench |
| ② 给 CSP 加 `'unsafe-eval'` → 拒签 | 把 4 处 `script-src 'self'` 全改成 `script-src 'self' 'unsafe-eval'` | `Error: … Content-Security-Policy allows 'unsafe-eval': … this gate refuses to certify a page by weakening the policy`；WebServer 启动即失败，**0 个测试运行、页面一次都没打开**，退出码 1 |
| ④ 四处 CSP 改 1 处 → 红 | 只在 `location ^~ /assets/` 那处追加 `https://cdn.invalid` | `Error: deploy/web/nginx.conf declares inconsistent Content-Security-Policy headers (directive 3 of 4 differs from the first) …`，退出码 1 |
| ③ 只修一半（一个入口）→ 红 | admin 用修后 dist、workbench 用修前 dist | `1 failed, 6 passed`：admin ✓，workbench ✗ `rendered no body text`，退出码 1 |

②的意义我另外做了对照，确认它不是形式主义：**同一份修前 admin dist，把 `script-src 'self'` 换成 `script-src 'self' 'unsafe-eval'` 后，真实 Chromium 渲染出正文 1208、`h1=["运行事实管理"]`、0 console error**。即"改策略"确实能让 #840 变成绿，而门禁拒绝认证这种绿。这是本卡最重要的保护。

③的"源码级只修一半不可构造"说法**成立**：`git show 7ce24bba --name-only` 在 `web/(admin|workbench|shared)/src` 下改动数为 **0**，两个入口都经 `@speechnexus/contracts` 这一个共享包解析契约，修一半需要同时改共享包又留一条旧路径，不是自然构造；门禁按两个独立 `dist` + 两个端口断言，所以"两个都被覆盖"由产物级反例证明，不依赖该说法的说服力。

### 4. 体积回归能不能接受？——MVP 可接受，但必须记为跟踪项，且"按 operation 分块"不是改构建配置能完成的事

实测（首屏实际传输，Chromium 记录 `response.body()`）：

| | 修前原始 / gz | 修后原始 / gz | 增量 (gz) |
| --- | --- | --- | --- |
| admin 首屏 | 754,953 / **175,689** | 2,585,620 / **302,144** | +126 KB (**+72%**) |
| workbench 首屏 | 861,079 / **179,268** | 3,284,609 / **314,112** | +135 KB (**+75%**) |

按"dist 内全部 js+css 求和"同一口径复算为 admin 175→299 KB、workbench 261→423 KB，与作者自报的 179→303 / 267→426 同基（±4 KB 为构建期差异），**+69%/+60% 的口径可复现**。

判断：**MVP 接受**。理由是绝对量（内网管理台 302/306 KB gz）在可接受区间，且这是 (B) 方案的固有成本——不预编译就必须 `'unsafe-eval'`，二者不可兼得；(B) 是唯一不削弱安全边界的解。但两个前提必须补：

- 代价要**写成跟踪项**并带上拆分成本，否则下一个人只会看到"多出来的 126 KB"而不知道为什么会重演。
- "按 operation 分块"的成本被低估了。我按模块量了体积：`workbench-http.validators.generated.mjs` 2.68 MB 原始 / **176 KB gz**、`workbench.validators` 898 KB / 78 KB、`file.validators` 696 KB / 73 KB、`admin-http` 590 KB / 48 KB、`admin.validators` 588 KB / 57 KB（29 个模块合计 11.8 MB 源码）。ajv standalone 的产物是**每个 bundle 一个扁平模块**，`$defs` 的共享校验函数只发一次；要按 operation 切块，必须让生成器额外产出"共享 $defs 模块 + 每个 entrypoint 模块"，再把 `validateRequest/validateResponse` 从同步改成可惰性加载（`createValidatedClient` 的中间件是同步 `onRequest/onResponse`），属于生成器 + 运行时 API 的改造，不是 `manualChunks` 一行配置。

首屏是否全量付费：**是**。修后 workbench 首次加载只有 1 个 JS 文件 **3,260,507 B**（`index-*.js`，gz 306 KB），`workbench-http` 的 500 个校验函数全在其中；admin 同理（1 个文件 2,585,620 B）。也就是说，首屏只用到少数几个 operation，却为 52 个 operation 的响应校验器全额付费。修前相反：ajv 编译器单独在 `errors-*.js`（46 KB gz）里，按需共享。所以这次不是"校验器从无到有"，而是"从按需编译改成全量内置"，代价位置从运行时移到首屏下载与解析。

## 三、逐条判定

### A. 修法是否真的消灭了 eval

- **A1 独立验证 bundle 无求值路径：通过（表述需更正）。** 证据见"核心四问 1"。补充：`Function(` 修前 1 次、修后 0 次；`eval(` 两侧 0；字符串定时器两侧 0；28 个生成物 `require(` 0 次。更正点：作者写的"`new Function` 0 次"在修前也不为 0 次是 `new` 这个词的问题——修前产物是 `Function(...)` 调用形式，若照抄"grep `new Function`"会得到修前修后都是 0 的假绿。
- **A2 浏览器实测 + 修前对照：通过。** 我自己起静态服务（独立实现，只从 `nginx.conf` 取 CSP 与 `location /` 的 `add_header` 集合，`/v1`、`/v2` 返回可解析 503），Chromium 打开两个入口：修前 0/0 + `EvalError`，修后 1208/752 + h1 + 0 pageerror + 0 console error。数字与作者自报**完全一致**。修前 `dist` 由 `origin/main`（`f7ee17ae`）现构建，对照可信。
- **A3 第二条 eval 路径：通过。** `web/*/src`、`packages/contracts/src` 全量 grep：无 `new Function`、无 `eval(`、无 `Function(`、无字符串 `setTimeout/setInterval`、无 `new RegExp(变量)`、无 `.compile(`。唯一运行期"编译"是生成物里对字面量正则的 `new RegExp("…","u")`（模块初始化，非求值，CSP 不限制）。唯一的裸 `Function(` 命中是 `profile-runtime.mjs` 的注释。

### B. format 是否静默降级

- **B4 负例与机制：通过（机制已答清，但无测试守护）。** 我独立复现三段：
  - 生产生成器 + `addFormats`：`good=true, bad(2026-13-45T99:99:99Z)=false`；
  - 复制生成器注释掉 `addFormats(ajv)`：`good=true, bad=true`（负例有牙，作者说法成立）；
  - 未注册 format：`exit=0`、stderr 两条 `unknown format "nexus-unregistered-format" ignored in schema at path …`、生成物里**没有** format 检查、`validators.Thing({at:'not-a-date-at-all'})===true`。
  - 现状保险度：bundle 用到的 `date-time/ipv4/ipv6/binary` 全在 ajv-formats `fullFormats` 内。
  - **风险点**：`capture_output=True` 把上述警告吞掉；且全仓 `grep 2026-13-45 | date-time 负例` 找不到任何已提交断言（`packages/contracts/tests`、`tests/contract`、fixtures 均无非法 format 值）。作者自报的负例是手工实验，不能防止未来回归。
- **B5 测试与生产同路：通过（例外安全）。** 6 个测试改为直接 import 生成的 `validators`（`profile-public-http`、`runtime-http` 等）。唯独 `coordination.schema.json` 走测试专用 `tests/support/ajv-profile-parser.mjs`。核实：`coordination` 在 `contracts/bindings.json` 中**不是 profile**（顶层独立段），`packages/contracts/src` 下**没有** `coordination.ts`/`coordination.validators.generated.mjs`，全仓 TS/JS 消费者只有 4 个测试文件本身（`coordination.test.mjs`、`leases.test.mjs`、`recovery.test.mjs`、`runtime-coordination.test.mjs`）。**没有浏览器/生产消费者**，因此该例外不构成"测试与生产不等价"，也不会把 eval 带回任何产物。测试专用 parser 的注释也已写明原因。

### C. 生成链与漂移守护

- **C6 生成物进 `--check`：通过。** `generate-contract-bindings.py` 的 `generate_standalone_validators()` 返回 `[*.mjs, *.d.mts]`，并被 `generate_profile()`（`outputs = [..., *validators, ...]`）、`generate_http_profile()`（`artifacts = (schema, *validators, wrapper, types)`）、`generate()` 的 `file_validators` 纳入；`--check` 逐字节比较每个 output。实测（PR head 快照）：
  - 干净树：`Contract bindings: up to date`，EXIT=0（14.7 s）；
  - 手改 `workbench.validators.generated.mjs`（追加一行注释）：`Generated contract drift: src/speechnexus_contracts/workbench.validators.generated.mjs; run make contracts-generate`，EXIT=1；
  - 手改 `admin.validators.generated.d.mts`：同上，路径为 `…admin.validators.generated.d.mts`，EXIT=1。
  - 附带发现（非本 PR 缺陷、低优先）：`--check` 只遍历"应生成"的路径，**孤儿产物不会被发现**——我放了一个 `orphan.validators.generated.mjs` 进去，`--check` 仍报 up to date。
- **C7 可复现：通过。** 在 PR head 快照上 `generate-contract-bindings.py` 连跑两次，对 `packages/contracts` 下 259 个文件做 sha256 快照：run1 与 run2 **完全一致**；而且"生成前 == run1"，即**已提交的生成物就是生成器的输出，没有手改**。`source-inventory.json` 由同一 `generate()` 产出并纳入 outputs，随 drift 一并逐字节校验；其与 yaml 的一致性因此由"生成即校验 + 字节相等"保证（我未单独重算 yaml 摘要，见第五节）。
- **C8 `require` → `import` shim：通过（三个模块、四条通路都验过）。** shim 形如 `ns.default && ns.default.__esModule === true ? ns.default : ns`。实测各通路下的取值：
  - Node ESM（`packages/contracts` cwd）：三个 specifier 的 `ns.default.__esModule === true` → 取 `default`（= CJS `module.exports`），`.default` 与 `.fullFormats` 都可用；
  - 打包后：产物里是 `_.fullFormats["date-time"]` / `func0 = __cjs2.default`，且在真实 Chromium + CSP 下**行为正确**（format 拒绝非法值、`maxLength` 走 `ucs2length` 生效）——不是"能加载"而是"语义对"；
  - Node 直跑生成物：`validators.RenderGenerationV2({...})` 正确返回 true/false；
  - Vite dev（esbuild pre-bundle，作者注释里担心的场景）：dev server 起在 5399，浏览器内 `profileGood=accepted, profileBad=rejected, httpBad=rejected`，0 error。
  - 稳健性：三条通路都能解析，是因为 ajv 系模块同时满足"`module.exports.__esModule` 为真"和"named export 可探测"；若上游改写形态，最坏表现为取到 namespace 而非 exports，两条用法（`.default` / `.fullFormats`）仍成立——风险有限但不能说已被生成期断言守护（没有任何测试断言 shim 的取值分支）。

### D. 门禁是否够硬

- **D9 双重保护：通过。** 见"核心四问 3"，两条独立复现，并附"放宽 CSP 会让修前构建变绿"的对照。
- **D10 四处 CSP 一致性：通过。** 改 `location ^~ /assets/` 一处 → `declares inconsistent Content-Security-Policy headers (directive 3 of 4 differs from the first)`，EXIT=1。解析器要求 `>=4` 处声明、彼此逐字相等、`location /` 与全文一致，三件都做了。
- **D11 admin/workbench 双覆盖：通过。** 产物级反例：只把 workbench 换回修前 dist → 仅 workbench 红。源码级"只修一半"确实不可自然构造（`web/*/src` 改动 0 处，共享包单点），但这不影响结论强度。
- **D12 CI 真跑：通过（看的是步骤日志，不是"作业绿"）。** `gh pr checks 842` 全绿、head 一致（`7ce24bba`）；`gh run view 43697780512 --job 132762160353 --log` 中确有 `Run pnpm test:deployed` → `Running 7 tests using 2 workers` → 逐条 `✓`（含两条 `first paint under the deployed policy`）→ `7 passed (1.5s)`；随后还有上传 `web-deployed-headers-evidence` 的步骤。
  - 本地同样 `7 passed (788ms)`。
  - 步骤位置正确：在 `pnpm build`、`pnpm verify:web-image`、`a11y`、`behavior` 之后，端口 4280/4281 与预览门禁 4173/4174 不冲突。

### E. 体积回归

- **E13 判断：MVP 可接受，但须跟踪 + 记录拆分成本。** 详见"核心四问 4"。补两点代价判断：① 首屏单文件 2.5 MB / 3.3 MB 原始，解析+编译成本没有测量（门禁只跑本地静态服务，localtime 首屏断言不测时长）；② 拆分收益上限有限——把 `workbench-http` 按 operation 惰性化后首屏仍要背 `file.validators`(73 KB gz)、`workbench.validators`(78 KB gz) 与首屏真正用到的少数 operation，乐观估计 306 KB → 150–200 KB gz 量级，不是回到 179 KB。
- **E14 首屏是否全量付费：是。** 实测首次加载 1 个 JS 文件（workbench 3,260,507 B / gz 306 KB；admin 2,585,620 B / gz 302 KB），500 个 HTTP 校验函数与 55 个 profile 校验函数全在其中；`realtime-*.js`(438 KB / 44 KB gz) 是惰性块，不在首屏。即**当前确实在为首屏用不到的校验器付费**。

### F. 管道性改动是否藏了东西

- **F15 忽略生成物是否会藏真问题：需要但是可以更窄。** `.prettierignore` 与 `eslint.config.js` 的忽略都**收在 `packages/contracts/src/speechnexus_contracts/*.validators.generated.{mjs,*}` 这个目录+后缀**上，不是整树放行，范围本身没问题。实测必要性：对 `workbench.validators.generated.mjs` 跑 `eslint --no-ignore` 得到 **2074 errors**（`no-redeclare` 1226、`no-unused-vars` 400、`no-useless-assignment` 346、`no-unreachable` 101、`no-unused-expressions` 1），全是 Ajv codegen 的产物风格；`prettier --ignore-path /dev/null --check` 也直接失败。所以忽略是对的。
  - 但"忽略"确实丢了**语法错误**与**未定义引用**这两类真问题（生成器只写文件不求值；drift 只比字节；只有测试 import 时才解析）。更窄的做法是让生成器在每个产物顶部写 `/* eslint-disable no-redeclare, @typescript-eslint/no-unused-vars, no-useless-assignment, no-unreachable */` 并**保留 lint 的解析与 `no-undef`**；`.prettierignore` 那条约等于"生成器末尾只格式化 `.json/.ts`"（`generate()` 第 949 行的后缀集合）造成的，也可以把 `.mjs` 加进那次 prettier 调用然后删掉忽略项——代价是每次生成多格式化 11.8 MB 源码、drift 输出变 noisy。两种都比现状窄，建议至少做前一种。
  - 另：`.d.mts` 被 lint 忽略无害，因为 `pnpm typecheck` 会检查它们（我本地 `typecheck` EXIT=0）。
- **F16 范围：干净。** 113 个文件分布：`packages/contracts` 100、`tests/web` 4、`scripts` 2、`playwright.deployed.config.mjs` 1、`package.json`/`eslint.config.js`/`.prettierignore`/`.github` 各 1、`docs` 2。**`nginx.conf` 改动 0 处**（作者"未改 CSP"成立）、`web/*/src` 改动 0 处（无业务逻辑）、`backend/·services/·node-controller/·execution-gateway/·migrations/` 改动 0 处、无 `deploy/` 改动。生成物无手改（C7 已证）。`package.json` 只加脚本与 lint 目标（未新增依赖，lockfile 未动，与"ajv 是既有依赖"一致）。

### G. 例数与 CI

- **G17 例数：全部对上。** 我在 PR head 上实跑：workbench **1064 / 65 文件**、admin **151 / 19**、`@speechnexus/contracts` **2241 pass**、`tests/ci` **404 passed / 1357 subtests**（在 `7ce24bba` 的 git worktree 内跑；在没有 `.git` 的快照目录里会有 4 条 routing 用例因 `git ls-files` 失败，属环境差异，不是缺陷）、`tests/contract/test_generated_bindings.py` **200 passed**、`test:deployed` **7**、`test:behavior` **18**、`test:a11y` **52**；`typecheck` / `lint` / `format:check` 均 EXIT=0；`gh pr checks 842` 全绿。
- **G18 漏改消费者：迁移干净。** 全仓 `createProfileParser(` / `createHttpValidator(` 调用点全部带第二个参数（我按"单参数调用"正则扫，命中项都是 `validators` 变量本身，无一处仍传 bundle 给 parser）；`packages/contracts/src` 下除生成物外**没有任何 `ajv` / `ajv-formats` import**（运行期编译路径确实清零）；`scripts/dev-env/workbench-manuscripts-probe.mjs` 已改为 `createHttpValidator(bundle, workbenchHttpValidators)`，且它同时用 `runtime.mjs` 的 `parse`（同样走预编译）。没有发现第三处未迁移的非类型检查消费者。

## 四、必须修清单

1. **【必须】给 format 不降级加已提交的守护。** 至少两条：① 一条负例测试（可用现有 fixture 派生）断言某个 `date-time` 字段的非法值让**生成物**返回 false——修前/修后都能跑，且注释写明"删掉 `addFormats` 就会变绿"；② 生成期断言：扫描 `contracts/` 里出现的所有 `format` 值，必须都在 `ajv-formats` 的 `fullFormats` 内，否则 `generate-contract-bindings.py` 失败。②直接消灭"未来静默降级"。同时把 `subprocess.run(..., capture_output=True)` 的 stderr 在失败或出现 `unknown format` 时透出。
2. **【必须】把三处自报措辞改准，避免把错误事实写进历史与 README。** ①产物里修前是 `Function(...)` 构造而非 `new Function`，用 `grep "new Function"` 会得到两侧都 0 的假对照；②"ajv 与 schema JSON 已从产物中移除"应改为"ajv **编译器**已移除；`ajv/dist/runtime/{equal,ucs2length}.js` 与 `ajv-formats/dist/formats.js` 仍被 import 并随产物内联"（我实测产物里 `validateFunctionCode` 0 次、`fullFormats` 3 次）；③`packages/contracts/README.md` 的 "ajv and ajv-formats are build-time inputs" 与 `package.json` 把它们留在 `dependencies` 的事实冲突，应写成"生成期需要编译器；产物 import 其 runtime helper 与 format 表"。
3. **【必须】体积回归进跟踪项。** 记录 +72%/+75%（首屏实际传输）与首屏单文件 2.5/3.3 MB 原始这一事实，并写明按 operation 分块的真实成本（生成器需拆"共享 $defs 模块 + 每 entrypoint 模块"，且 `validateRequest/validateResponse` 需从同步改为可惰性加载），排期或明确接受"不拆"。缺这一条，下一个人只会看到无解释的 +126 KB。

建议（非阻塞）：`eslint.config.js` 的忽略改由生成器在产物首行写 `eslint-disable` 规则白名单（保留解析与 `no-undef`）；`.prettierignore` 改为在生成器里格式化 `.mjs` 后删除忽略项；drift 增加孤儿产物检测；门禁可选增加一条"预编译校验器确实被执行"的断言（当前由 2241 条契约测试覆盖语义，门禁只保证"无求值 + 能渲染"，两者叠加已足够，但分离在两个门禁里）。

## 五、只能依赖作者陈述的点

以下我**没有**独立复现，判定时依赖作者陈述或间接证据：

1. **`PASS` 与 Ajv standalone 产物的等价性论证**：我验证了 format 与若干结构约束（required/additionalProperties/pattern/maxLength/oneOf）在产物上生效，但没有把 2241 条契约测试逐条对"修前 ajv 编译"与"修后生成物"做行为差分。等价的强证据是"选项相同 + 2241 条测试全绿"，不是逐 case 双向差分。
2. **CI 上的 `Integration 6/8` 曾因漏改 probe 失败并已 amend**：我核到今天 `7ce24bba` 上 Integration 6/8 绿、probe 源码已迁移，但**没有**回放上一版 CI 的失败日志，无法确认"确实只是这一个消费者漏改"。
3. **容器/镜像里实跑**：作者明确声明门禁不依赖 Docker，也不覆盖 `always` 语义、`location` 内 `add_header` 继承、重复头、`envsubst`、真实上游与 TLS。我沿用了该边界，**没有**起容器验证真实 nginx 复现（按约束也不应在本机起容器）。因此"镜像里真绿"仍是未被本审查覆盖的最后一环，只有"部署配置声明的策略下浏览器能首屏"这一层被我实测。
4. **`source-inventory.json` 与 yaml 的逐条摘要一致**：我只确认它由同一生成器产出、随 drift 逐字节校验、且重生成无差异；没有独立重算 yaml→摘要。
5. **`git diff origin/main...HEAD --stat` 只含本报告**：这是本报告的落盘约束，我在提交前核对过；被审内容一律走 `git show 7ce24bba:`，未污染审查分支。

## 六、过程记录（时间线）

| 时间 | 动作 | 结果 |
| --- | --- | --- |
| 17:20 | `git fetch`、确认 `docs/reviews/2026-11-15-csp-eval-standalone.md` 不存在、建 worktree `/private/tmp/sn847`、读 4 份治理文件 | 基线 `f7ee17ae`，报告名可用 |
| 17:21 | `git archive 7ce24bba` → `/private/tmp/sn847-head`；两树 `pnpm install --frozen-lockfile`（779 ms / 777 ms） | 依赖就绪 |
| 17:23 | 两树 `pnpm build` | 均 EXIT=0；修前 dist 772K/1.2M，修后 2.5M/3.8M |
| 17:24 | 产物 grep（带阴性对照） | 修前 `Function(`=1，修后 0；两侧 `eval(`=0 |
| 17:26 | 自建静态服务（读 `nginx.conf` 的 CSP 与 `location /` 头，`/v1`/`/v2` 503）+ 4 个 origin 起服务；Chromium 插桩探针 | 修前 0/0+EvalError；修后 1208/752，h1 正确，应用侧 Function 0 次 |
| 17:28 | 用同一 Vite 8/rolldown 打包评审探针（`probe/main.ts` → 2,503.06 kB / 168.45 kB gz） | 与作者 2.6 MB/165 KB 自报一致 |
| 17:29 | 探针在 CSP 下运行 + 生成器 format 三段实验（含注释掉 `addFormats`） | 非法 date-time 被拒；去 addFormats 变 true；未注册 format 静默通过 |
| 17:31 | format 清单核查（30 bundle / 4 种 format vs `fullFormats` 27 项） | 现状无未注册 format |
| 17:33 | `generate-contract-bindings.py --check`（venv python，14.7 s） | 干净树 up to date；改 `.mjs`/`.d.mts` 均红并指名路径；孤儿产物不报 |
| 17:34 | 连跑两次生成 + 259 文件 sha256 快照 | run1==run2，生成前==run1（可复现、无手改） |
| 17:35 | `pnpm test:deployed` 干净跑 | 7 passed |
| 17:36 | 换修前 dist → 门禁 | 2 failed（body 0） |
| 17:38 | 只换回 workbench 修前 dist | 1 failed（仅 workbench） |
| 17:39 | 改 1/4 处 CSP → 红；4 处全加 `unsafe-eval` → 拒签、0 测试运行 | 两条反例成立 |
| 17:40 | 修前 dist + 放宽 CSP 的对照 | 渲染 1208 → 证明"放宽换绿"诱惑真实存在 |
| 17:41 | workbench 1064/65、admin 151/19、contracts 2241、tests/ci 404（`7ce24bba` worktree）、bindings 200、behavior 18、a11y 52、typecheck/lint/format | 全部 EXIT=0，例数与自报一致 |
| 17:42 | `gh pr checks 842` + Web 作业日志 | 全绿；日志确认 `test:deployed` 步骤执行且 7 passed |
| 17:43 | 范围分类、`eslint --no-ignore` 噪音统计、首屏网络计量 | 无 CSP/业务/后端改动；2074 lint errors；首屏单文件 2.5/3.3 MB |
| 17:45 | 写报告、`uv run --no-project ruff format --check`、核对 `git diff origin/main...HEAD --stat` | 见文末 |

## 七、复现命令（关键几条）

```sh
# 修前/修后对照（真实 CSP）
node /private/tmp/sn-review/serve.mjs /private/tmp/sn847/web/admin/dist      5280 /private/tmp/sn847-head/deploy/web/nginx.conf
node /private/tmp/sn-review/serve.mjs /private/tmp/sn847-head/web/admin/dist 6280 /private/tmp/sn847-head/deploy/web/nginx.conf
node /private/tmp/sn-review/probe.mjs http://127.0.0.1:6280 AFTER-admin

# 门禁四条反例（在 PR head 快照内，改完即还原）
cd /private/tmp/sn847-head && pnpm test:deployed
#  ① 换修前 dist      ② 只换 workbench 修前 dist
#  ③ 改 1/4 处 CSP    ④ 4 处全加 'unsafe-eval'

# 生成链
python3 scripts/dev-env/generate-contract-bindings.py --check
# 手改任一 *.validators.generated.{mjs,d.mts} 后重跑 → 红
```

审查结论：**方向正确、核心性质成立、可合入；三条必须修（format 守护、事实表述、体积跟踪项）应在合入前或紧随合入完成。**
