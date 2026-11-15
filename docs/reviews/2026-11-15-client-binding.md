# PR #824 非作者审查：生成契约包成为显式依赖并使用 exports（fix #822）

- 审查对象：PR #824 `fix/822-client-binding`，head `ade3da2813dd4f1a7a2fc121d8b9eaadf98ec29b`，自报修 #822
- 基线：`origin/main` = `1c3d09d32bd828add18d03fe24bf16c5cc4d9de9`（`test: flake 复现 harness 落地 + 修相邻租约窗口用例 (#813) (#815)`）
- 审查分支：`review/826-client-binding`，`git worktree add /private/tmp/sn826 -b review/826-client-binding origin/main`
- 运行检出：`/private/tmp/sn826-pr`（detached `ade3da28`，跑守卫、单测、build、typecheck、浏览器用例与反例变异）、`/private/tmp/sn826-mut`（`rsync` 副本，专门跑守卫绕过电池，无 `.git`）。两者不写回产品代码，审查结束清理
- 环境：macOS arm64、Node `v26.12.1`（与 `.nvmrc` 的 `26.12.1` 一致）、pnpm `12.14.0`、Python 3.14.9
- 审查时间：2026-11-15 10:27–11:05（Asia/Shanghai）
- 被审内容一律用 `git show ade3da28:<path>` 读取；`/private/tmp/sn826` 上只有本报告。所有数字为本机实测，标注「作者自报」的除外
- 报告文件名预检：`origin/main` 的 `docs/reviews/` 共 55 个文件，`2026-11-15` 当天已有 8 个报告，无 `client-binding`、无 `822` 相关命名，本文件名未被占用

## 结论

**不通过（必须修 2 项后复核）。**

交付主体是成立的：#822 的「36 处非测试 import 全部改经包清单」确实做到（`web/` 内已无一处相对路径进入 `packages/contracts/src/**`），依赖与 lockfile 声明正确，两个 schema 子路径补上了原先「相对能过、包路径无入口」的缺口，`web/*/dist` 与基线**逐字节相同**，作者自报的四类反例我独立复现了三类半（重命名、删依赖、新增无消费者子路径、导出目标移动），例数与 CI 全绿也都对得上。

但本卡最关键的两点没有成立，且都落在本 PR 新增的守卫/登记资产上：

1. **登记表是万能豁免（本卡最需要修的地方）。** 守卫对 `export-consumers.json` 只校验「值是非空字符串」（`validate-contract-consumers.mjs:231-234`），既不要求写明可定位的消费方，也不校验该消费方存在或真的导入对应生成模块。实测：把任意新子路径 `./ghost` 指向任意已存在文件，再写一条「TOTALLY BOGUS: no consumer anywhere」即 **PASSED**；把理由写成引用一个**不存在的** Python 文件同样 **PASSED**。也就是说，任何人往这张表里写一行就能让任意新 `exports` 过关，规则 4 的左半边（无消费者必须登记「真实理由」）在机器上不存在。
2. **19 条理由里有 1 条是虚假登记，正好命中「掩盖死契约」。** `./zero-dispatch` 的理由称「Python 侧只由 `tests/contract/test_zero_dispatch_contract.py` 经生成包做契约校验」，而该测试**没有导入生成包的任何模块**（它读 `contracts/bindings.json`、用 `tests/support/contracts.py` 的 `jsonschema` 校验源文件、并用 `generator()` 在内存里打 bundle），全仓 1465 个 `.py` 与所有 TS 中**没有任何代码导入** `speechnexus_contracts.zero_dispatch`。其余 18 条经机器逐条核对为真（见 §C）。

另有 7 项守卫可被绕过或产生假红，其中 3 项属于「能写进正常代码的形态」（非字面量动态 `import()`、`.jsx`/`.cjs` 扩展名、字符串字面量被误判为 import），建议本轮一并收口，但按严重度列为建议修。

## 判定总表

| 项 | 内容 | 判定 | 证据 |
| --- | --- | --- | --- |
| A1 | 守卫四类规则能否被绕过 | 部分成立：主路径能红，存在 7 类逃逸/假红 | §A |
| A2 | 登记表能否当万能豁免 | **不成立（必须修）**：只验非空字符串 | §B |
| A3 | 过期登记是否会红 | 成立：真实消费者出现即红 | §D.1 |
| B4 | 19 条登记理由真实性抽样 5 条 | 4 真 1 假（`./zero-dispatch`） | §C.2、§C.3 |
| B5 | `./zero-dispatch` 是合理登记还是掩盖死契约 | **掩盖死契约 + 理由失实（必须修）** | §C.3 |
| C6 | 反例是否真红 | 成立：①③④独立复现，②的失败形态符合声明 | §D |
| D7 | bundle 与基线逐字节相同 | 成立：`diff -r` 无输出 | §E.1 |
| D8 | `exports` 指向 src 的约定与第二份产物 | 成立：既有约定一致，无第二产物 | §E.2 |
| E9 | diff 范围与生成产物 | 成立：47 files/+681/−65，无生成产物改动 | §F.1 |
| E10 | 例数与 CI | 成立：本机全绿，CI 全绿，`changes` 作业里守卫实跑 | §F.2 |

## 审查过程（时间线）

| 时点 | 动作 | 结果 |
| --- | --- | --- |
| T0 10:27 | `git fetch` 得 `origin/main=1c3d09d3`；建 `/private/tmp/sn826`（审查分支，基于 `origin/main`）与 `/private/tmp/sn826-pr`（detached `ade3da28`） | 两棵树互不干扰，都不基于被审分支拉审查分支 |
| T1 10:28 | 读 `web/AGENTS.md`、`contracts/AGENTS.md`、`docs/validation/ci-validation.md`（后两者用 head 版本比对 diff）；读守卫 341 行、单测 178 行、`export-consumers.json`、两份 `package.json` 与 lockfile diff | 确定四条规则的实现方式全是正则 + 文本扫描，无 AST |
| T2 10:29 | PR 树跑 `node --test tests/ci/contract-consumers.test.mjs` 与守卫 | 14/14 通过、`[PASSED]`；守卫扫 338 个代码文件耗时 0.116 s（`changes` 作业预算 3 min） |
| T3 10:30–10:36 | 两棵树 `pnpm install --frozen-lockfile`；`/private/tmp/sn826-mut` 上跑 12 组守卫变异（登记豁免、过期登记、非字面量动态 import、模板字符串 import、`.jsx`/`.cjs`、symlink、shim 链、字符串假红、导出目标越界、嵌套条件导出、伪消费者、alias 形态） | 见 §A、§B |
| T4 10:36–10:40 | 在 `/private/tmp/sn826-pr` 上施加 4 组产品侧反例并复原（重命名已用子路径、删依赖、新增无消费者导出、导出目标移动） | 见 §D |
| T5 10:39 | 用脚本把 19 条登记逐条机器核对（引用路径是否存在、是否真的导入被引用的生成模块），再人工深核 5 条 | 18 真 1 假 |
| T6 10:31–10:40 | 两棵树 `pnpm build`；`diff -r` 比对 `web/*/dist`；PR 树跑 `typecheck`/`lint`/`format:check`/`test`/`test:behavior`/`test:a11y`；`origin/main` 树跑 `python3 -m unittest discover -s tests/ci` | 见 §E、§F |
| T7 10:38 | `gh pr checks 824`；拉 `changes` 作业日志确认新步骤在 CI 里真的执行 | 全部 `pass`；日志见 §F.2 |
| T8 11:00 | 写本报告，`uvx --from "ruff==0.17.2" ruff format --check .`、`git diff origin/main...HEAD --stat` 复核 | 见 §F.3 |

## A. 守卫能否被绕过（本卡最重要）

### A.0 实现方式

守卫不是 AST，是「先 `withoutComments` 掩掉注释，再用三条正则抓 specifier」：

```js
// scripts/dev-env/validate-contract-consumers.mjs:58-71
const SPECIFIER_PATTERN = /^@speechnexus\/contracts(?:\/|$)/;
const IMPORT_PATTERNS = [
  /(?:^|\n)[ \t]*(?:import|export)\b[\s\S]*?\bfrom\s+["']([^"']+)["']/g,
  /(?:^|\n)[ \t]*import\s+["']([^"']+)["']/g,
  /\bimport\s*\(\s*["']([^"']+)["']/g,
];
```

- 「导入方」= 扫描根 `web/`、`scripts/`、`tests/`、`packages/` 下扩展名属于 `CODE_EXTENSIONS` 的文件（`.ts .tsx .mts .cts .mjs .js`）。
- 「最近包清单」= `owningPackage()` 从文件所在目录向上找第一个带 `name` 的 `package.json`，再取 `dependencies`/`devDependencies`/`peerDependencies`。

### A.1 规则 1（`exports` 目标必须是包内已存在文件）：成立，但不验「生成产物」

正例与负例都对：

```text
# exports 目标越出包目录 → 红（且同时报无消费者）
"./escape": "../shared/src/index.ts"
[CONTRACT-CONSUMER-ERROR] packages/contracts/package.json:1
  Rule Violation: Export './escape' targets a missing generated artifact '../shared/src/index.ts'.
  Rule Violation: Export './escape' has no consumer. ...
```

但这条规则只做两件事：目标解析后在包目录内、`fs.existsSync` 且是普通文件。它**不校验目标是生成产物**（新加 `"./ghost": "./src/realtime.ts"` 也能过规则 1），这一点直接影响 §B 的豁免成本。另外文档写「每个目标必须是包内已存在的**生成**文件」（`docs/validation/ci-validation.md` 新增表格），实际语义只是「已存在文件」，属措辞超前。

健壮性缺陷：`exports` 支持嵌套条件对象，而 `exportTarget()` 只取一层，拿到对象后直接喂给 `path.resolve`，守卫以未捕获 TypeError 崩溃而不是给出可读错误：

```text
"./nested": { "import": { "default": "./src/runtime.ts" } }
TypeError [ERR_INVALID_ARG_TYPE]: The "paths[1]" argument must be of type string.
    at Object.resolve (node:path:1257:7)
    at runValidation (…/validate-contract-consumers.mjs:203:27)
```

失败方向是 fail-closed（退出码 1），不算假绿，但报错形态会误导排查。

### A.2 规则 2（`web/**` 不得相对导入 `packages/contracts/src/**`）：主形态成立，四类逃逸

成立的部分（`.ts`、`export * from`、多行 import 都能红）：

```text
# web/workbench/src/zz-rel.ts:1
import { parse } from "../../../packages/contracts/src/runtime.mjs";
→ Web code imports the generated package by relative path '…'; import '@speechnexus/contracts' or …

# web/workbench/src/zz-reexport.ts:1
export * from "../../../packages/contracts/src/runtime.mjs";
→ 同上

# 多行 import 同样红
```

逃逸的写法（每条都实测，守卫 **PASSED**）：

| 逃逸形态 | 写法 | 为何逃逸 |
| --- | --- | --- |
| 扩展名缺口 | `web/workbench/src/zz-rel.jsx` 里相对导入 `packages/contracts/src/runtime.mjs` | `CODE_EXTENSIONS` 不含 `.jsx`/`.cjs`，文件根本不被读 |
| CJS + require | `web/workbench/src/zz-rel.cjs` 里 `require("../../../packages/contracts/src/runtime.mjs")` | 扩展名不在列表；且三条正则都不匹配 `require(...)` |
| symlink | `web/workbench/src/vendor/contracts -> ../../../../packages/contracts`，再 `import … from "./vendor/contracts/src/runtime.mjs"` | 只做 `path.resolve` 词法解析，不 `realpath`，解析结果不在 `contractsDir` 内 |
| 转发链（两跳） | `scripts/dev-env/zz-bridge.mjs` 相对导入 `packages/contracts/src/…`（非 `web/`，规则 2 不适用），`web/workbench/src/zz-shim.ts` 再相对导入该 bridge | 规则按**文件**判定而非按解析链；`web/` 的那一跳落在 `scripts/`，不落进 `packages/contracts` |
| alias 形态 | `import { parse } from "@contracts/runtime"` | 不以 `.` 开头、不匹配 `SPECIFIER_PATTERN`，整条规则对它不存在。当前 `tsconfig*.json` 无 `paths`、`vite.config.*` 无 alias，属未配置形态，但守卫也不读这些配置，未来加 alias 会静默失效 |

`symlink` 与转发链需要有人刻意写出奇怪代码才能触发，严重度低；`.jsx`/`.cjs` 是真实的规则缺口（仓库现在没有这类文件，规则却宣称覆盖 `web/**`）。

### A.3 规则 3（导入方最近包清单必须声明 `workspace:*`）：成立

反向对照：在声明了依赖的包里删掉依赖行、并删掉 pnpm 建立的 `@speechnexus/contracts` 链接后，守卫报 26 条（与作者自报一致）；未声明依赖的包（`web/shared`）引入根 specifier 立刻红：

```text
[CONTRACT-CONSUMER-ERROR] web/shared/src/zz-shared-consumer.ts:1
  Rule Violation: '@speechnexus/contracts' is imported without declaring
  "@speechnexus/contracts": "workspace:*" in web/shared/package.json.
```

逃逸口在 specifier 形态：非字面量动态 import 完全不被看见，因此「有导入但没声明依赖」可以整体逃过规则 3（见 A.4）。

### A.4 规则 4（两端对齐）：过期登记成立，「消费者」定义过宽，动态 import 全逃逸

**过期登记确实会红**（本卡要求独立复现的一条）：

```text
# 给已登记无消费者的 ./runtime 加一个真实消费者
web/workbench/src/zz-stale.ts:1  import { parse } from "@speechnexus/contracts/runtime";
→ Export './runtime' is registered as unbound but is consumed by
  web/workbench/src/zz-stale.ts:1; remove the stale registration.
```

`export * from "@speechnexus/contracts/runtime"`（命名导出形态）同样被计为消费者并触发同一条红——说明 `export … from` 分支是被覆盖的。

**「消费者」的定义宽到可以橡皮图章。** 规则 4 只要求「扫描到一个经包清单的 import」，不校验导入的绑定被使用、也不校验运行时会执行：

```text
# 新增 exports "./ghost3"，再在 web 里写一行 no-op 副作用导入
import "@speechnexus/contracts/ghost3";
→ PASSED（规则 4 认为它有消费者，无需登记）
# 只写类型导入同样算消费者（且会反过来把已登记项判成过期登记）
import type { components } from "@speechnexus/contracts/runtime";
→ Export './runtime' is registered as unbound but is consumed by …
```

即「两端对齐」的机器含义是「出现过字符串」，不是「有真实使用」。这与 README 新段落「Every declared subpath either has a real consumer…」的措辞有距离，建议在文档里显式写明该口径（`ci-validation.md` 已写了「不证明运行时解析/打包产物/页面调用」，但没有写「不证明导入被使用」）。

**非字面量动态 import 全逃逸**（同时也击穿规则 3）：

```text
web/workbench/src/zz-dyn.ts
export async function load(name: string) {
  const spec = `@speechnexus/contracts/${name}`;
  return import(spec);
}
export const any = (spec: string) => import(spec);
→ PASSED

web/workbench/src/zz-dyn2.ts
export const load = () => import(`@speechnexus/contracts/ghost`);
→ PASSED   # ./ghost 未声明，仍不红
```

反方向还有一类**假红**：守卫把字符串/模板字面量内容里的 `import … from "…"` 当成真 import（`withoutComments` 只掩注释、保留字符串内容），因此合法代码里贴示例或夹具文本就会红：

```text
web/workbench/src/zz-string.ts
export const snippet = `
import { parse } from "@speechnexus/contracts/ghost";
`;
export const legacy = 'import x from "@speechnexus/contracts/also-ghost";';
→ 2 条 "Specifier '…ghost' is not a declared exports subpath"
```

这一条属于「会打正常代码」的形态（例如把契约示例贴进源码注释块、错误文案模板、测试夹具字符串），且行号定位不可靠：三条正则都允许 `[\s\S]*?` 跨语句，报出的行是**一段连续语句的起点**而不是 import 那一行（上面第 3 行 import 的文件被报成 `:1`）。`contract-consumers.test.mjs` 里「commented-out specifiers are not consumers」只覆盖了注释方向，没覆盖字符串方向。

### A.5 绕过清单汇总

| # | 形态 | 结果 | 严重度 |
| --- | --- | --- | --- |
| 1 | 登记表写任意非空理由 | 不红（§B） | **必须修** |
| 2 | 新增无消费者子路径 + 橡皮图章 no-op/type-only import | 不红 | 高（规则 4 语义空洞） |
| 3 | 非字面量/模板字面量动态 `import()` | 不红（规则 2/3/4 全失明） | 高 |
| 4 | `.jsx`、`.cjs`、`require()` | 不红（规则 2/3 失明） | 中 |
| 5 | 字符串/模板字面量里出现 import 文本 | **假红** | 中 |
| 6 | symlink、非 web 转发链、alias specifier | 不红 | 低（需刻意构造/当前未配置） |
| 7 | 嵌套条件导出对象 | 崩溃（fail-closed） | 低（诊断质量） |

## B. 登记表能否当万能豁免（本卡核心之一）

### B.1 代码事实

守卫对登记表只跑这一段：

```js
// scripts/dev-env/validate-contract-consumers.mjs:227-234
for (const [key, reason] of Object.entries(unboundRegistry)) {
  if (typeof reason !== "string" || reason.trim() === "") {
    errors.push({ …, message: `Unbound export '${key}' needs a non-empty reason.` });
  }
  if (!entries.has(key)) { … /* 登记了未声明的子路径 */ }
}
```

`export-consumers.json` 的 schema 是 `{"unbound": {"<subpath>": "<字符串>"}}`：**没有任何字段承载消费方**，因此守卫连「消费方存在」这个概念都无从校验。`tests/ci/contract-consumers.test.mjs` 的 `a registered unbound export needs no consumer` 用的就是 `"Python consumer only; no Web surface yet."` 这种不指向任何实体的理由，测试本身把「不校验」固化成了契约。

### B.2 实证

```text
# M1：新增 exports "./ghost"（指向任意已存在文件）+ 登记任意理由
"./ghost": "./src/realtime.ts"
"unbound": { "./ghost": "TOTALLY BOGUS: no consumer anywhere, no real reason" }
→ [PASSED] Generated-contract exports, consumers and workspace dependencies are aligned.

# M2：把理由写成引用一个不存在的 Python 文件
"unbound": { "./ghost": "Python consumer: backend/src/…/DOES_NOT_EXIST.py via speechnexus_contracts.ghost" }
→ [PASSED]
```

### B.3 判断与修法

这是本卡最需要修的地方：规则 4 的「无消费者 → 必须登记理由」这条约束，机器侧等价于「登记表里有这个 key 且值非空」。**任何人往表里加一行即可让新 `exports` 过关**，登记表成为无限额豁免；同时它也让 §C 的虚假登记静默通过（该表存在 219 天没人会去逐条核对是真还是假）。

可落地的收口（任一即可显式降级风险，推荐前两条都做）：

1. 结构化登记 + 机器校验：值改为 `{"reason": "…", "consumer": {"path": "<repo 相对 .py/.mjs>", "module": "speechnexus_contracts.<m>"}}`；守卫校验 `path` 存在、是文件，且文件内容含 `speechnexus_contracts.<m>`（并对 `m` 做子路径规范化推导：`realtime-events` → `realtime_events`，`x-http` → `x_http`），不匹配即红。这套推导对 18 条现有登记全部成立（§C.1），对 `./zero-dispatch` 会红——正好是它该红的那条。
2. 退一步：要求 `consumer.path` 存在且为仓库内文件（只做存在性），至少能拦住「理由引用不存在实体」这一类。
3. 在 `tests/ci/contract-consumers.test.mjs` 里补三条负例：理由为空（已有）、理由引用不存在的路径、声明的消费方文件不导入对应模块。

## C. 19 条登记理由的真实性（本卡核心之二）

### C.1 全表机器核对

对 19 条逐条抽取「被引用的生成模块」与「被引用的消费方文件」，再检查该文件是否真的导入该模块：

- 18 条成立：引用的 `backend/`、`execution-gateway/`、`tests/` 路径都存在，且文件里确有对应 `speechnexus_contracts.<module>` 导入。
- 1 条不成立：`./zero-dispatch` 没有引用任何生成模块（理由文本里没有模块名），被引用的测试文件也不导入生成包。

### C.2 抽样 5 条（本卡指定）

| 登记 | 引用的消费方 | 证据 |
| --- | --- | --- |
| `./execution` | `backend/src/speechnexus_backend/speech/infrastructure/admission_postgres.py` | `:14  from speechnexus_contracts.execution import parse`；另有 37 个 `.py` 导入该模块 |
| `./voice-catalogue-http` | `backend/.../speech/application/voice_capability_evidence.py` | `:30  from speechnexus_contracts.voice_catalogue_http import validate_response`；共 13 个 `.py` 导入 |
| `./profile-public-http` | `backend/.../speech/interfaces/http/profile_contract.py` | `:7  from speechnexus_contracts.profile_public_http import contract as public_contract`；共 7 个 `.py` 导入 |
| `./runtime` | `tests/contract/test_runtime_http_bindings.py` | `:11  from speechnexus_contracts.runtime import parse`。注意：全仓只有这 1 个文件导入 `speechnexus_contracts.runtime`，所以理由里的「Python 控制器链路消费」是**过度措辞**（实际消费者是契约测试；`runtime_controller_http.py` 导入的是 `runtime_http` 而非 `runtime`），但括号里点名的文件是真的，属可接受的措辞瑕疵 |
| `./zero-dispatch` | `tests/contract/test_zero_dispatch_contract.py` | **不成立**，见 C.3 |

### C.3 `./zero-dispatch`：虚假登记，且掩盖死契约

理由原文（`packages/contracts/export-consumers.json:21`）：

> `"TS 入口无外部消费者：零派发是默认关闭的候选契约，没有生产者或网络客户端；Python 侧只由 tests/contract/test_zero_dispatch_contract.py 经生成包做契约校验，Web 不使用。"`

事实：

1. 该测试不导入生成包。它的依赖是 `tests/support/contracts.py`（`jsonschema` 直接校验 `contracts/**` 源文件）与 `tests/contract/test_binding_entrypoints.py` 的 `generator()`（在内存里打 bundle）；`contracts/bindings.json` 里 `zero_dispatch` 的配置是 `client: false`，`test_zero_dispatch_contract.py:98` 读的是 `manifest["profiles"]["zero_dispatch"]` 这段 JSON，不是生成产物。
2. 全仓没有任何代码导入 `speechnexus_contracts.zero_dispatch`（生成文件存在，`zero_dispatch.py` 只被自己的 `zero_dispatch_models` 引用）。TS 侧同理：没有 `web/` 文件导入 `@speechnexus/contracts/zero-dispatch`，它靠登记才没让守卫变红。
3. 所以 `./zero-dispatch` 是**两端都没有消费者的死契约**，登记把它伪装成「Python 有消费者」。

判断：这不是「合理登记」，而是「用一条听起来合理的理由掩盖了死契约」。虚假登记比没有登记更糟——它让「19 条已登记」这个数字本身不可信，也掩盖了「这个导出是否应该继续存在」的产品问题。可选修法（需契约/产品口径决定）：把理由改成事实陈述（例如「候选契约，`client: false` 且默认关闭，TS/Python 两侧均无消费者，保留导出仅为发布面完整性」），或者干脆删掉该 `exports` 条目（`exports` 是手工维护的，生成器不写它；删条目不会破坏生成产物，但会让生成文件 `zero_dispatch.ts` 暂时无入口，需与契约负责人确认）。

其余 18 条我按 §C.1 的方法逐条核过，全部有真实导入（`./runtime` 的措辞已在 C.2 单列）。

## D. 反例是否真的红

### D.1 重命名被使用的子路径 → `TS2307`（声明解析失败，不再是路径不存在）

```text
# 把 exports 的 "./realtime-events" 改名为 "./realtime-events-renamed"（目标不变）
$ node scripts/dev-env/validate-contract-consumers.mjs
[CONTRACT-CONSUMER-ERROR] web/workbench/src/realtime/asr-stream.ts:1
  Rule Violation: Specifier '@speechnexus/contracts/realtime-events' is not a declared exports subpath …
[CONTRACT-CONSUMER-ERROR] web/workbench/src/realtime/realtime-client.ts:2
[CONTRACT-CONSUMER-ERROR] web/workbench/src/realtime/tts-stream.ts:1
[CONTRACT-CONSUMER-ERROR] packages/contracts/package.json:1
  Rule Violation: Export './realtime-events-renamed' has no consumer.
[FAILED] Contract consumer binding validation failed with 4 error(s).

$ pnpm --filter @speechnexus/workbench typecheck
src/realtime/asr-stream.ts(1,39): error TS2307: Cannot find module '@speechnexus/contracts/realtime-events' or its corresponding type declarations.
src/realtime/realtime-client.ts(3,37): error TS2307: Cannot find module '@speechnexus/contracts/realtime-events' …
src/realtime/tts-stream.ts(1,39): error TS2307: Cannot find module '@speechnexus/contracts/realtime-events' …
（其余为 TS2307 之后类型退化成 any 的级联错误）
```

失败形态与作者声明一致：首要错误是**包声明解析失败**（改名前是 `../../../../packages/contracts/src/realtime_events` 这种「找不到文件」的形态）。这条也顺带证明「同版」的收益是真实的。

### D.2 删依赖 → 守卫 26 错 + tsc 94 错（与作者自报一致）

```text
# 删掉 web/workbench/package.json 的 "@speechnexus/contracts": "workspace:*"，
# 再删掉 pnpm 的 node_modules 链接（否则 tsc 仍能从残留链接解析）
$ node scripts/dev-env/validate-contract-consumers.mjs 2>&1 | grep -c CONTRACT-CONSUMER-ERROR
26
$ pnpm --filter @speechnexus/workbench typecheck 2>&1 | grep -c "error TS"
94
```

第一条样例：`'@speechnexus/contracts/workbench-http' is imported without declaring "@speechnexus/contracts": "workspace:*" in web/workbench/package.json.`

### D.3 新增无消费者子路径 → 红（作者反例③）

```text
"./ghost": "./src/realtime.ts"
→ Export './ghost' has no consumer. Bind it in web (or a workspace consumer),
  or register the reason in packages/contracts/export-consumers.json.
[FAILED] … failed with 1 error(s).
```

（注意：这条红只值一行理由就能消掉，见 §B。）

### D.4 `exports` 指向已移动产物 → 红（规则 1）

```text
"./execution": "./src/execution_moved.ts"
→ Export './execution' targets a missing generated artifact './src/execution_moved.ts'.
[FAILED] … failed with 1 error(s).
```

## E. 「同版」与产物

### E.1 bundle 与基线逐字节相同（独立复现）

```text
$ cd /private/tmp/sn826      && pnpm build   # origin/main
$ cd /private/tmp/sn826-pr   && pnpm build   # ade3da28
$ diff -r /private/tmp/sn826/web/workbench/dist /private/tmp/sn826-pr/web/workbench/dist ; echo EXIT:$?
EXIT:0
$ diff -r /private/tmp/sn826/web/admin/dist /private/tmp/sn826-pr/web/admin/dist ; echo EXIT:$?
EXIT:0
$ du -sh web/workbench/dist web/admin/dist
1.2M  web/workbench/dist
772K  web/admin/dist
```

无新增 chunk、无重复依赖副本、无体积变化。作者声明成立。

### E.2 `exports` 指向 TS 源码的约定与「第二份产物」

`packages/contracts` 没有 build 步骤（`package.json` 只有 `typecheck`/`test`），全部 28 条既有 `exports`（含 `"."`）本来就指向 `./src/*.ts`；本 PR 新增的两条 `./schemas/workbench`、`./schemas/file` 指向 `./src/speechnexus_contracts/*.schema.json`，同样是 `src/` 下的生成产物。因此：

- 与仓库既有约定一致，不是本 PR 新造的例外；
- `git diff --name-only origin/main ade3da28 -- packages/contracts` 只有 `README.md`、`export-consumers.json`、`package.json` 三个文件，没有新增 dist 或第二份产物；
- 两个新子路径的唯一消费方是 `web/workbench/src/books/render-unit-bounds.test.ts`（测试用生成 schema 钉住 UI 边界），改前它用相对路径进入 `contracts/src`，会被规则 2 判红，所以补这两个 `exports` 是必要的而不是凑数。

一个副作用值得记录：`render-unit-bounds.test.ts` 同时把 `parse` 的导入从 `contracts/src/runtime.mjs` 改成根入口 `"@speechnexus/contracts"`。根入口 `src/index.ts` 是 `export { ContractValidationError, parse } from "./runtime.mjs"` 的再导出，`parse` 同一实现，语义未变（typecheck 与 1063 个 workbench 用例都过）。

## F. 范围与例数

### F.1 diff 范围（E9）

```text
$ git diff origin/main...ade3da28 --stat | tail -1
 47 files changed, 681 insertions(+), 65 deletions(-)
```

- 类别与 PR 声明一致：13 个 `web/**` 源文件 + 12 个 `web/**` 测试/夹具 + 两个 `web/*/package.json`、守卫脚本与 14 条单测、`export-consumers.json`、`packages/contracts/{README.md,package.json}`、lockfile（+6 行 workspace link）、`ci.yml`（+5 行新步骤）、三份文档。
- **没有手动改生成产物**：`git diff --name-only origin/main ade3da28 | grep -E "generated|\.schema\.json|models\.py|parsers\.py|source-inventory"` 为空；`packages/contracts` 下未触碰任何 `src/**` 生成文件，符合 `contracts/AGENTS.md` 的「不手改生成产物」。
- `web/` 侧除 import 行外没有逻辑改动：把 diff 的 `+/-` 行去掉 import/注释行后为空；`speech-file-client.ts` 的 +5/−10 是两行注释改写 + 一处 import 合并，不是行为变化。
- `pnpm-lock.yaml` 只多两条 importer 的 workspace link（+6），无其他解析变化。
- 版本探针：`scripts/dev-env/validate-contracts.py` → `contract validation: passed (37 files)`；生成文件未变动，故本审查未重复跑 `contracts-drift`（需要完整 uv 同步，见 §G）。

### F.2 例数（本机实测）

| 检查 | 结果 |
| --- | --- |
| `pnpm typecheck` | 5 个 workspace 全部 `Done`，EXIT 0 |
| `pnpm lint` | 设计 Token 守卫 `[PASSED]`、`ESLint: passed`，EXIT 0 |
| `pnpm format:check` | `All matched files use Prettier code style!`，EXIT 0 |
| `pnpm build` | EXIT 0（两棵树都跑，见 §E.1） |
| `pnpm test` | EXIT 0；`web/workbench` **1063 passed / 65 files**、`web/admin` 151 passed / 19 files、`packages/contracts` 全过 |
| `pnpm test:behavior` | **18 passed**（18.0 s） |
| `pnpm test:a11y` | **52 passed**（19.1 s） |
| `node --test tests/ci/contract-consumers.test.mjs` | **14 pass / 0 fail** |
| `python3 -m unittest discover -s tests/ci`（在 `origin/main` 树） | **Ran 370 tests … OK**（本 PR 不改 Python，故在基线树跑；本机用仓库 `.venv` 的 Python 3.14.9） |

顺序按 CI 约束执行：`test:behavior` 在 `pnpm build` 之后跑；`test:behavior` 与 `test:a11y` 串行，不并发。

`gh pr checks 824`：全部 `pass`（含 `Scope routing and CI / design-token checks` 22 s、`Web: lint, types, tests, build and accessibility` 3m25s、`Python: tests, coverage and package build` 6m25s、六分片 Integration 与各 Gate），`Docs: markdown code block formatting` 为 `skipping`（路径不触发）。新步骤在 CI 里确实执行：

```text
Scope routing and CI / design-token checks  … ##[group]Run node --test tests/ci/contract-consumers.test.mjs
… node scripts/dev-env/validate-contract-consumers.mjs
… ℹ tests 14
… ℹ pass 14
… Checking generated-contract consumer bindings...
… [PASSED] Generated-contract exports, consumers and workspace dependencies are aligned.
```

## 必须修清单

1. **登记表的豁免必须可校验（本卡最需要修的地方）**。当前只验「非空字符串」（`validate-contract-consumers.mjs:227-234`），任意理由可让任意新 `exports` 过关（§B.2）。改为结构化登记（`reason` + `consumer.path`/`consumer.module`）并校验路径存在、文件确实导入对应生成模块；或至少要求登记项引用仓库内存在的文件。补三条负例单测。
2. **`./zero-dispatch` 的登记理由失实**（`export-consumers.json:21`）：被引用的 `tests/contract/test_zero_dispatch_contract.py` 不导入生成包，全仓无任何代码导入 `speechnexus_contracts.zero_dispatch`。把理由改成事实（两侧均无消费者、候选契约 `client: false`），或与契约负责人确认后删除该 `exports`；不能以「Python 有消费者」的名义继续登记。
3. （与 1 同批）**规则 4 的「消费者」口径必须在文档中写明**：「扫描到一个经包清单的 import 即计为消费者，不校验绑定被使用、不证明运行时会执行」。现在 README 的「real consumer」与实现不一致（一个 no-op 副作用导入或 `import type` 就够，实测）。

## 建议修清单（非阻塞，按性价比排序）

1. **字符串/模板字面量误判为 import（会打正常代码）**：`withoutComments` 只掩注释、保留字符串内容，导致代码里贴示例文本即假红（实测 2 条）。建议在匹配前把字符串/模板字面量的**内容**也掩掉（保留 specifier 位置），或对匹配结果做「不在字符串内」的二次判定；补一条「字符串里的 import 不是消费者、也不报未声明子路径」的单测。
2. **非字面量动态 import 全失明**：`import(spec)` / 模板字面量形态既不参与规则 3 也不参与规则 4。至少加一条「`web/**` 内出现非字面量 `import(` → 红或明确告警」，否则「有导入但没声明依赖」存在一条静默通道。
3. **扩展名缺口**：`CODE_EXTENSIONS` 不含 `.jsx`/`.cjs`，`require()` 也不识别；`.jsx` 可被 Vite 编译，属真实入口。补扩展名与 `require` 形态，或在文档里写明限制。
4. **symlink 逃逸**：规则 2 只做词法 `path.resolve`，`web/**` 内指向 `packages/contracts` 的 symlink 不红。改成 `realpath` 后比较即可。
5. **嵌套条件导出对象崩溃**：`exportTarget` 返回对象时 `path.resolve` 抛 TypeError。改成报一条可读错误（并补单测）。
6. **错误行号不可靠**：正则允许跨语句匹配，报出的行是一段语句的起点（实测把第 3 行的 import 报成 `:1`）。修复方式与建议 1 同源（先精确切出 import 语句再定位）。
7. **文档措辞**：`docs/validation/ci-validation.md` 表格里的「`exports` 每个目标必须是包内已存在的**生成**文件」，实现只校验「已存在文件」；建议改为「已存在文件」或补上生成产物来源校验。

## 只能依赖作者陈述或未完全验证的点

- 作者自报的「47 files, +681/−65」「lockfile +6 行」「26 错 + 94 错」「1063/65 files」「18」「370」等例数我都独立复核过，均一致；`changes` 作业里守卫实跑也已从 CI 日志确认。**没有**依赖作者陈述的结论项。
- 未重复执行 `make contracts-drift`（需要完整 `uv sync` 与生成器依赖）：依据是生成产物零改动（§F.1）与 `validate-contracts.py` 通过，因此 drift 状态与基线相同；这条属「由未变的输入推断」，不是直接实测。
- `python3 -m unittest discover -s tests/ci` 在 `origin/main` 树上跑（本 PR 零 Python 改动）；若要求在 PR 树上重跑，需要为 `/private/tmp/sn826-pr` 建独立 `.venv`，本审查未做。
- 未验证其他操作系统/Node 版本下的行为：CI 是 ubuntu-24.04 + `.nvmrc` 的 Node 26.12.1，本机同为 Node 26.12.1/macOS arm64；Windows 路径分隔符分支未实测（`toPosix` 看起来正确，但无证据）。
- 作者 PR 正文的「27 个 exports 子路径一个都没被用」：基线实际是 **28 个 key**（`"."` + 27 个子路径），若把 `"."` 排除在外则口径自洽；这一点不影响结论，仅记录差异。
