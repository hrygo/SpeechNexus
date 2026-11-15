# PR #824 返修增量审查：契约消费者登记改为可校验证据（#822）

- 审查对象：PR #824 `fix/822-client-binding` 的**返修提交** `6451aa25ae4cd5378f18d12fbadfd83157e0fdb3`（父提交 `ade3da28`），自报修 #822；同 PR head 即 `6451aa25`
- 基线：`origin/main` = `07d9081efb03e2d9cab1e080d051be17d93883c6`（`docs: 记录 PR #825 制品保留期与清理的非作者审查 (#827)`）
- 审查分支：`review/828-guard-rework`，`git worktree add /private/tmp/sn828 -b review/828-guard-rework origin/main`
- 运行检出：`/private/tmp/sn828-head`（detached `6451aa25`，跑守卫、单测、`pnpm test`、Python 单测、ruff）、`/private/tmp/sn828-mut`（`rsync` 副本，无 `.git`，专跑变异电池）。两者不写回产品代码
- 环境：macOS arm64、Node `v26.12.1`（与 `.nvmrc` 一致）、pnpm `12.14.0`、Python 3.14.9、ruff 0.17.2
- 审查时间：2026-11-15 11:14–11:36（Asia/Shanghai）
- 被审内容一律用 `git show 6451aa25:<path>` 读取；`/private/tmp/sn828` 上只有本报告
- 范围：**只审返修 diff `ade3da28..6451aa25`（6 文件 / +1115 / −242）**。前一轮已合入的报告 [`2026-11-15-client-binding.md`](2026-11-15-client-binding.md)（`89d56e89`）不重做；其中已成立的两项（36 处导入改经包清单、`web/*/dist` 与基线逐字节相同）本轮不复测，只核「本轮未改动 web/ 源码与生成产物」
- 报告文件名预检：`origin/main` 的 `docs/reviews/` 共 57 个文件，`2026-11-15` 当天已有 10 个报告，无 `guard-rework`、无 `828` 相关命名，本文件名未被占用

## 结论

**不通过（必须修 3 项后复核）。**

返修把上一轮 3 条必须修**按字面逐条做到了**，且做法扎实：登记改为结构化后，上一轮那三条负例（消费方路径不存在 / 存在但不导入 / 只写 `reason`）本轮独立复现**全部红**，连「值就是字符串」的原始攻击形态也红；`./zero-dispatch` 的理由改为事实陈述，我按文档与 `contracts/bindings.json` 逐句核对**全部属实**，「全仓 0 导入」也为真；消费者口径已在 README 与 `ci-validation.md` 双处写明。上一轮 7 条建议修（字符串/模板误判、非字面量动态 `import()` 与 `require()`、`.jsx`/`.cjs`、`realpath`、嵌套条件导出可读错误、多行 import 行号、`ci-validation.md` 措辞）**全部修好并实测有效**，`./runtime` 的措辞也改准。例数全部对得上（29 / 68 / 370 / 1063+65），CI 里守卫实跑。

但**本卡的核心风险并没有消除，只是换了一个入口**：

1. **`consumer:null` + `retention` 是新的零成本豁免。** 上一轮的问题是「往表里写一行字符串就能给任意新 `exports` 盖章」。现在同一件事只需要往表里写一个 `retention` 块——而且可以**从 `./zero-dispatch` 整块复制**（连写的是 DEL-417、与目标子路径毫无关系的 `decision` 文本一起）。实测四条全部 `[PASSED]`：任意新 `./ghost` 盖章（R1）、`basis` 换成存在但无关的 `README.md`（R2）、`basis` 指向**目录**（R3）、`binding` 换成不相干的 `realtime` profile（R4）。`binding` 必须是 `client:false` 这一条不够约束——`contracts/bindings.json` 15 个 profile 里有 **11 个** `client:false`，且守卫不校验该 profile 与子路径有任何对应关系。判断：**是把豁免从一张表换成了另一张表**（详见 §B）。
2. **消费方证据只查 Python 源码文本，不做注释/字符串遮蔽，双向都错。** 反向：在任意真实 `.py` 里加一个 docstring `from speechnexus_contracts.ghost import parse`，登记即 `[PASSED]`（N5）；正向：在 `tests/contract/test_zero_dispatch_contract.py` 的 docstring 里写使用示例，`./zero-dispatch` 立刻被误报成「registered as consumerless but … imports」（FR-5，**合法写法被判红**）。同一个根因还带来第二处：守卫**不校验推导出的模块真的存在**，所以「新建 `exports` + 写一个导入幻影模块的测试文件」即可 `[PASSED]`（N4）。
3. **新规则对一种正常写法产生假红：无插值的模板字面量。** ``import(`@speechnexus/contracts/runtime`)`` 是**静态可判定**的说明符，却被报成「template-literal specifier … must be statically checkable」（FR-2）。文档只写「非字面量或模板字面量失败」，读者会理解成「带插值的模板」，实现把无插值模板一并判死。

另外两条不阻塞但应记入建议：符号链接把整个生成包镜像进 `web/` 时报告会爆炸成 242 条（其中 241 条指向 `web/…/vendor/contracts/src/*.ts` 这类检出上并不存在的「文件」）；`retention.basis` 只做 `existsSync`，目录也放行。

## 判定总表

| 项 | 内容 | 判定 | 证据 |
| --- | --- | --- | --- |
| R1 | 前一轮必须修 1：登记表万能豁免 | **部分修好**：三条原负例全红，但 `retention` 成为等价零成本豁免 | §A.1、§B |
| R2 | 前一轮必须修 2：`./zero-dispatch` 理由失实 | **修好**：理由与 retention 内容逐句属实，0 导入为真 | §E |
| R3 | 前一轮必须修 3：消费者口径未写明 | **修好**：README + `ci-validation.md` 双处写明，且与实现一致（Python 证据一侧除外，见 §A.3） | §A.4 |
| Q1 | 消费方路径不存在 / 存在不导入 / 只写 reason 是否真红 | **成立**：三条 + 原始字符串形态共 4 红 | §A.1 |
| Q2 | 自造「更难骗」负例：测试文件当消费者 | **放过（PASSED）**；判断：可接受但与「模块必须存在」应一并收口 | §A.2 |
| Q3 | 自造「更难骗」负例：`retention.basis` 伪造 | **放过（PASSED）** | §B.2 |
| Q4 | 推导规则可被利用：不存在的模块 / 非 Python 子路径 | 前者放过；后者只能走 `retention`（即 Q3 的豁免） | §C |
| Q5 | `consumer:null`+`retention` 是否新万能豁免 | **是**（程度降低，类别未变） | §B |
| Q6 | 5 条新规则的反方向风险（假红） | 全仓 0 假红；探针命中 2 条假红（FR-2、FR-5） | §D |
| Q7 | `./zero-dispatch` 事实陈述准确性 | **全部准确** | §E |
| Q8 | web/ 源码与生成产物本轮未改 | **成立** | §F.1 |
| Q9 | 例数与 CI | **成立**（CI 为 24 pass + 1 skipping，非「25 项全绿」） | §F.2 |

## 审查过程（时间线）

| 时点 | 动作 | 结果 |
| --- | --- | --- |
| T0 11:14 | `date`、`git status`、`git fetch` 得 `origin/main=07d9081e`；确认 `docs/reviews/2026-11-15-guard-rework.md` 不存在 | 基线确定；工作区只有他人的 `uv.lock` 未提交改动，未触碰 |
| T1 11:15 | `git worktree add /private/tmp/sn828 -b review/828-guard-rework origin/main`；`git diff origin/main...HEAD --stat` 为空 | 硬要求起点满足 |
| T2 11:15 | `git worktree add /private/tmp/sn828-head --detach 6451aa25` | 被审内容检出 |
| T3 11:16 | 读返修 diff：`export-consumers.json`、守卫 794 行（原 341）、单测 457 行（原 178）、`README.md`、`ci-validation.md`、`web/AGENTS.md` | 确定实现仍是零依赖词法扫描 + 字符级遮蔽，不是 AST |
| T4 11:16 | PR 树跑守卫与 `node --test tests/ci/contract-consumers.test.mjs` | `[PASSED]`（0.20 s / 338 文件）；29/29 |
| T5 11:17 | `rsync` 建 `/private/tmp/sn828-mut`；备份 `package.json`/`export-consumers.json` 作基线 | 变异环境就绪 |
| T6 11:17–11:21 | **MF-1 电池**：三条原负例（N1 路径不存在、N2 存在不导入、N3 只写 reason、N3b 原始字符串形态）、两条自造（N4 测试文件当消费者、N5 docstring 伪造导入） | 4 红 + 2 放过 |
| T7 11:21–11:22 | **retention 电池**：R1 任意新 exports 整块复制、R2 无关 `basis`、R3 `basis` 为目录、R4 无关 `client:false` `binding` | 4 条全 `[PASSED]` |
| T8 11:22 | **推导规则**：`./ghost`→`speechnexus_contracts.ghost`（不存在）、`./schemas/workbench`/`./schemas/ghost` 非 Python 子路径 | 见 §C |
| T9 11:22 | `./zero-dispatch` 事实核对：文档 status/六个入口/不生成客户端、`bindings.json` 的 `client:false`、全仓 Python/TS 导入计数、生成模块与导出 | 逐条属实 |
| T10 11:23 | `pnpm install --frozen-lockfile`（EXIT 0）、`pnpm test`、`node --test tests/ci/` | 1063/65 + 151/19、68/68 |
| T11 11:24 | **假红探针**：FR-1/1b 字符串·模板·正则·除法、FR-2 无插值模板动态 import、FR-3 合法 `.jsx`/`.cjs`、FR-4 含引号正则、FR-5 docstring 提及保留模块；另 NEW-1..4 验证五条新规则真生效 | 2 条假红（FR-2、FR-5），其余不误判 |
| T12 11:24 | 逃逸探针：ESC-1 非 ASCII 标识符 import、ESC-2 模板插值内 import、RES-1 两跳转发链 | 三条全 `PASSED`（均为前一轮已记录或本轮未纳入的形态） |
| T13 11:25 | `/Users/hrygo/Documents/SpeechNexus/.venv/bin/python -m unittest discover -s tests/ci` | `Ran 370 tests … OK` |
| T14 11:25 | `gh pr checks 824`、`gh pr view 824`、拉 `Scope routing and CI` 作业日志 | head 确为 `6451aa25`；24 pass + 1 skipping；日志里 29 例与守卫 `[PASSED]` 均出现 |
| T15 11:26 | `uv run --no-project ruff format --check .`（ruff 0.17.2） | `1872 files already formatted`，EXIT 0 |
| T16 11:28 | 写本报告；`git diff origin/main...HEAD --stat` 复核 | 只有本报告 |

## A. MF-1 是否真堵住

### A.0 返修改了什么（代码事实）

结构化登记的两条分支（`scripts/dev-env/validate-contract-consumers.mjs:462-597`）：

- `consumer` 为对象 → `checkConsumer()`：`path` 非空字符串、不越出仓库、**不在 `packages/contracts` 内**、`existsSync` 且 `isFile()`、扩展名必须 `.py`、`module`（若给出）必须等于 `pythonModuleFor(key)` 的推导值、且该文件文本必须命中 `from|import speechnexus_contracts.<m>` 正则（`:530-539`）。
- `consumer === null` → `checkRetention()`：`decision`/`enable`/`delete` 非空字符串、`basis` 存在（**仅 `existsSync`**）、`binding` 必须是 `contracts/bindings.json` 的 profile 且 `client === false`；再由 `findPythonImporters()` 实测该模块**无任何 Python 导入**（`:672-682`）。

其余（规则 4 的两端对齐、过期登记）逻辑未变。

### A.1 三条原负例：全部红（本卡硬要求 1）

每条都在干净基线上新增 `exports` `./ghost`（指向已存在的 `./src/runtime.ts`），只改登记项：

```text
# N1：consumer.path 不存在
[CONTRACT-CONSUMER-ERROR] packages/contracts/export-consumers.json:1
  Rule Violation: Export './ghost' consumer path 'backend/src/speechnexus_backend/speech/DOES_NOT_EXIST.py' does not exist.
[FAILED] Contract consumer binding validation failed with 1 error(s).   EXIT=1

# N2：consumer.path 存在（真实 .py）但不导入该模块
  Rule Violation: Export './ghost' consumer 'backend/…/speech/application/binding_documents.py' does not import speechnexus_contracts.ghost.
EXIT=1

# N3：只写 reason，不写 consumer
  Rule Violation: Export './ghost' must declare a consumer object or "consumer": null with retention.
EXIT=1

# N3b：完全复刻上一轮的攻击形态（值就是字符串）
  Rule Violation: Registered export './ghost' must be an object with 'reason' and 'consumer'.
EXIT=1
```

另测两条声明里的旁支，同样红：

```text
# consumer 落在生成包内
  Rule Violation: Export './ghost' consumer path 'packages/contracts/src/speechnexus_contracts/runtime.py' is the generated package itself, not a consumer.
# consumer 不是 Python 文件
  Rule Violation: Export './ghost' consumer 'web/workbench/src/realtime/realtime-client.ts' must be a Python file importing 'speechnexus_contracts.ghost'.
```

**判定：上一轮必须修 1 的三条负例确实堵住，且逐条与作者自报的校验项一一对应。**

### A.2 自造负例一：`consumer.path` 指向「存在且真导入」的**测试**文件 → 放过

```text
# 新增 exports "./ghost"（模块 speechnexus_contracts.ghost 在生成包里不存在）
# 登记 consumer = tests/contract/test_ghost_consumer.py，内容仅：
#   from speechnexus_contracts.ghost import parse
$ node scripts/dev-env/validate-contract-consumers.mjs
[PASSED] Generated-contract exports, consumers and workspace dependencies are aligned.   EXIT=0
```

守卫会放过，而且顺带暴露第二个缺口：**它不校验推导出的模块真的存在**。`packages/contracts/src/speechnexus_contracts/ghost.py` 不存在，导入根本不可能成立，登记仍然通过——因为证据是「文本里出现这串字符」。

判断（本卡要求给判断）：**「测试文件算消费者」本身可以接受**，理由有三条：一是作者自己的 `./runtime` 就是这么登记的（唯一导入方是 `tests/contract/test_runtime_http_bindings.py`），并在 `reason` 里诚实写明「只有契约测试」；二是 TS 侧同样把 `web/**/*.test.ts` 计为消费者，两侧口径一致；三是 `scripts/`、`tests/` 的探针与夹具本来就在门禁外（文档已写明）。但**「模块可以不存在」这一点不可接受**：它让「新建 exports + 写一个导入幻影模块的测试」成为零摩擦过门路径，性质和上一轮的「写一行理由」相同。建议补一行 `packages/contracts/src/speechnexus_contracts/<mod>.py` 的存在性校验；若不想读生成目录，至少要求 `module` 出现在生成包的 `__init__.py`/清单里。

### A.3 自造负例二：`consumer.path` 是真实 `.py`，但「导入」只在 docstring 里 → 放过（且反向假红）

```text
# 往 backend/src/speechnexus_backend/speech/application/binding_documents.py 末尾追加：
#   _DOC = """
#   from speechnexus_contracts.ghost import parse
#   """
# 登记 consumer.path 指向该文件
$ node scripts/dev-env/validate-contract-consumers.mjs
[PASSED] Generated-contract exports, consumers and workspace dependencies are aligned.   EXIT=0   （N5）
```

原因是 `checkConsumer()` 与 `findPythonImporters()` 都对 Python 源文件直接跑正则，**没有做注释/字符串遮蔽**（JS 侧有 `maskSource()`，Python 侧没有）。同一根因在反方向上是**假红**：

```text
# 在 tests/contract/test_zero_dispatch_contract.py 的模块 docstring 里写使用示例：
#   """
#   使用示例（文档）：
#       from speechnexus_contracts.zero_dispatch import parse
#   """
$ node scripts/dev-env/validate-contract-consumers.mjs
[CONTRACT-CONSUMER-ERROR] packages/contracts/export-consumers.json:1
  Rule Violation: Export './zero-dispatch' is registered as consumerless but tests/contract/test_zero_dispatch_contract.py imports speechnexus_contracts.zero_dispatch.
[FAILED] … 1 error(s).   EXIT=1   （FR-5）
```

这两条是同一处不对称：文档说「注释、字符串与模板正文中的 import 文本不计入消费者」，而实现只对 JS 成立。**判定：必须修。**

### A.4 消费者口径（前一轮必须修 3）

`docs/validation/ci-validation.md` 新增独立段落、`packages/contracts/README.md` 重写为「A registration is evidence, not an exemption」：

> **消费者口径**：出现一个经包清单的 import（静态导入、`import type`、副作用导入与字面量 `require()`）即计为消费者；守卫既不校验该绑定是否被使用，也不证明该导入在运行时会执行。注释、字符串与模板正文中的 import 文本不计入消费者。

TS 侧的口径与实现一致（FR-1b 实测：字符串/模板里的 import 文本不再误判，也不再计入消费者）。**判定：该条修好**；但「注释、字符串与模板正文中的 import 文本不计入消费者」这半句对 **Python 证据一侧不成立**（A.3），属文档与实现的第二处偏差。

## B. `consumer:null` + `retention` 是不是新的万能豁免（本卡核心）

### B.1 结构与约束

`checkRetention()` 的机器校验只有四条：`decision`/`enable`/`delete` 非空（`trim()` 后）、`basis` **存在**、`binding` 是 `bindings.json` 的 profile、该 profile 的 `client === false`。此外 `findPythonImporters()` 会实测该推导模块没有任何 Python 导入。

问题在于这四条**都不与子路径发生关联**：

| 校验项 | 实际约束强度 | 实测 |
| --- | --- | --- |
| `binding` 是 `client:false` profile | 15 个 profile 里 11 个满足（`realtime`、`realtime_events`、`scenario_profiles`、`model_bindings`、`builtin_voice_qualification`、`release_evidence`、`audio_inspection`、`zero_dispatch`、`gateway_recovery`、`binding_defaults`、`binding_defaults_producer`）；不要求该 profile 的 `source` 与子路径对应 | R4：`./ghost` + `binding: "realtime"` → `[PASSED]` |
| `basis` 是「已存在决定文档」 | 只 `existsSync`，不看内容、不看是否与子路径有关、也不要求是文件 | R2：`basis: "README.md"` → `[PASSED]`；R3：`basis: "docs/components"`（目录）→ `[PASSED]` |
| `decision`/`enable`/`delete` 非空 | 纯自由文本，一字即可 | 三字段填「随便」→ `[PASSED]` |
| 实测无导入 | 真校验（Python 侧受 A.3 影响；TS 侧靠过期登记规则） | 成立 |

### B.2 实验：给任意新 exports 盖章（本卡硬要求，含判断）

```text
# R1：新增 exports "./ghost"，把 ./zero-dispatch 的 retention 块整块复制过去（只改 reason）
#     —— 复制块里的 decision 讲的是 DEL-417 与 zero_dispatch，与 ./ghost 毫无关系
$ node scripts/dev-env/validate-contract-consumers.mjs
[PASSED] Generated-contract exports, consumers and workspace dependencies are aligned.   EXIT=0

# R2：在上面基础上伪造 basis 指向存在但无关的 README.md，decision/enable/delete 全填「随便」
[PASSED] …   EXIT=0

# R3：basis 指向「目录」docs/components（不校验 isFile）
[PASSED] …   EXIT=0

# R4：binding 换成不相干的 client:false profile "realtime"（与 ./ghost 无任何对应关系）
[PASSED] …   EXIT=0
```

即：**「新增一个没有任何消费者的 `exports`」这件事，仍然可以用零个新文件、十行 JSON 自我盖章**，成本与上一轮「写一行字符串理由」同阶（差别只是要复制一个块）。守卫的规则 4 本意是「无消费者的新子路径必须红」，`retention` 把这个红灯的代价压回接近零。

### B.3 判断：是把豁免从一张表换成了另一张表

**是**——类别未变，程度降低。逐条说：

- 好处是真实的：`retention` 不能给**活着的**契约盖章（一旦出现 TS 消费者或 Python 导入立刻红），所以它盖不出「假装有人用」的假象；`./zero-dispatch` 的案例从此必须明写「两侧都没有消费者」。这一点比上一轮的字符串理由强得多。
- 但「给死契约/新子路径无限期续命」的能力原样保留。`retention` 的语义其实就是「我宣布它是候选」，而机器无法判断这个宣布是否属实——它只判断你写没写、字段全不全。
- `binding` 必须是 `client:false` **不足以**约束：11/15 个 profile 满足，且不带任何「此 profile 对应此子路径」的关联。R4 用 `realtime` 给 `./ghost` 盖章即证。
- 真正的收口点在**让副本不可复用**。可落地的三条（都不需要引入 AST 依赖）：
  1. `retention.binding` 与子路径建立可校验关联：例如要求 `bindings.json` profile 的 `source` 与子路径同源（`./zero-dispatch` ↔ `internal/zero-dispatch.schema.json`），或要求 profile 名与子路径规范化后相等（`./x-y` ↔ `x_y`——现有 19 条里 `zero_dispatch` 满足，可作为起点）；
  2. 每个 profile 至多一个 `retention` 项（同 profile 第二次出现即红），直接掐死复制；
  3. `basis` 必须是文件，且正文出现该子路径字符串（`./zero-dispatch` 的文档确实写了 `@speechnexus/contracts/zero-dispatch`，R1 的复制块必然失败）。
- 另一条可选路线：承认 `retention` 是唯一的候选通道，把它限制为「只能用于 `contracts/bindings.json` 里本来就 `client:false` **且** 在 `docs/` 有 `status: proposed` 文档的 profile」，并把「新增 exports」与「retention」解耦——新建子路径一律先红，必须由同一次变更添加 profile。

## C. 推导规则是否可被利用

推导函数（`pythonModuleFor`，`:386-391`）：`./x-http` → `speechnexus_contracts.x_http`（`-`→`_`），并要求结果匹配 `^[a-z0-9_]+$`。全表 19 条逐一验证：

```text
./audio-inspection            -> speechnexus_contracts.audio_inspection            exists=True
./binding-defaults-http       -> speechnexus_contracts.binding_defaults_http       exists=True
./zero-dispatch               -> speechnexus_contracts.zero_dispatch               exists=True
…（19 条全部 exists=True）
```

**推导约定本身正确**：19 条推导出的 `.py` 在 `packages/contracts/src/speechnexus_contracts/` 全部存在。

C.1 **推导出不存在模块的子路径**：`./ghost` → `speechnexus_contracts.ghost`（`ghost.py` 不存在）。守卫**不校验模块存在**，因此只要有一个文本命中正则的文件即可过关（A.2 的 N4 是 `[PASSED]`）。**判定：可被利用，建议补一行存在性校验。**

C.2 **非 Python 子路径**（`./schemas/workbench`、`./schemas/file`，目标是 `src/speechnexus_contracts/*.schema.json`）：

- `pythonModuleFor("./schemas/workbench")` 因含 `/` 返回 `null`，于是**不能用 `consumer` 对象登记**：

  ```text
  Rule Violation: Export './schemas/workbench' has no derivable generated Python module.
  ```

- 它们现在**不需要登记**：`web/workbench/src/books/render-unit-bounds.test.ts:2` 经包清单导入，规则 4 已满足（这也是上一轮确认的「补 `exports` 是必要的」那两条）。
- 一旦失去消费者，它们**只能走 `consumer:null` + `retention`**。实测：新增 `./schemas/ghost`（目标为已存在的 `workbench.schema.json`）不登记 → 红（正确）；用 `retention` 块盖章 → `[PASSED]`。

```text
# ./schemas/ghost 无消费者、无登记
  Rule Violation: Export './schemas/ghost' has no consumer. Bind it in web (or a workspace consumer), or register the reason and evidence …
# 加上从 ./zero-dispatch 复制的 retention 块
[PASSED]
```

**判定：非 Python 子路径在「无消费者」情形下没有可校验的证据通道，只剩 B 节的豁免通道**——这让 `retention` 比表面上更承重。建议为非 Python 子路径补一条同等强度的替代证据（例如允许 `consumer.path` 指向 `.ts`/`.mjs` 且要求其中出现该 `@speechnexus/contracts/<subpath>` 说明符），而不是逼它们走 `retention`。

## D. 五条新规则的反方向风险（假红）

### D.0 全仓实测：0 假红

```text
$ /usr/bin/time -p node scripts/dev-env/validate-contract-consumers.mjs
Checking generated-contract consumer bindings...
[PASSED] Generated-contract exports, consumers and workspace dependencies are aligned.
real 0.16 / 0.14 / 0.14（三次）
扫描文件 338 个（`listConsumerFiles` 计数）
```

在现行代码上跑全仓守卫**没有出现任何假红**，耗时 0.14–0.20 s（`changes` 作业预算充裕）。三条新扩展名/路径机制在当前仓库的触达面：

- `.jsx`/`.cjs`：全仓只有 1 个 `.cjs`（`docs/architecture/diagrams/deployment-clusters-v3-4k/render_and_validate.cjs`），不在扫描根（`web`、`scripts`、`tests`、`packages`）内，因此新规则今天零触发；我另造合法 `.jsx`/`.cjs` 各一个（用包清单导入）→ `[PASSED]`（FR-3）。
- `realpath`：`git ls-files -s` 中符号链接数为 **0**，今天不影响任何文件。
- 遮蔽：注释/字符串/模板里的 import 文本不再误判（FR-1b `[PASSED]`），含引号的正则字面量也不会吞掉后面的 import（FR-4 `[PASSED]`）。

五条新规则的**正向**（该红的红）我也各测一次，全部生效：

```text
# NEW-1 非字面量 / 模板动态 import（web/）
web/workbench/src/zz-probe-dyn.ts:3  dynamic import with a non-literal specifier
web/workbench/src/zz-probe-dyn.ts:5  dynamic import with a non-literal specifier
web/workbench/src/zz-probe-dyn.ts:6  dynamic import with a template-literal specifier
# NEW-2 .cjs + require（相对路径进生成包 + 包清单导入计为消费者）
web/workbench/src/zz-probe-cjs.cjs:2  imports the generated package by relative path '…'
                                      + Export './runtime' is registered as unbound but is consumed by …cjs:1（正确：说明字面量 require 被计入）
# NEW-3 .jsx 相对导入生成包
web/workbench/src/zz-probe.jsx:1  imports the generated package by relative path '…'
# NEW-4 symlink 指向 packages/contracts 后相对导入
web/workbench/src/zz-probe-link.ts:1  imports the generated package by relative path './vendor/contracts/src/runtime.mjs'
# 行号
web/workbench/src/zz-probe-multiline.ts:8  …（多行 import 报的是说明符所在行，不是语句起点）
# 嵌套条件导出
Rule Violation: Export './nested' has no string import/types target (nested condition objects are not supported).（不再是 TypeError）
```

### D.1 假红一：无插值的模板字面量动态 import（FR-2）

```text
# web/workbench/src/zz-probe-tpl-static.ts
export async function load() {
  return import(`@speechnexus/contracts/runtime`);
}
$ node scripts/dev-env/validate-contract-consumers.mjs
[CONTRACT-CONSUMER-ERROR] web/workbench/src/zz-probe-tpl-static.ts:3
  Rule Violation: Web code uses dynamic import with a template-literal specifier; the contract binding must be statically checkable with a literal specifier.
[FAILED] … 1 error(s).   EXIT=1
```

``import(`…`)`` 无插值时是完全静态的说明符，Vite/tsc 都能解析；把它判成「不能静态校验」是**实现超出文档**的假红（README 举的例子带 `${name}`，文档正文写的是「必须带字面量说明符」）。**判定：必须修**——`maskSource()` 已经知道模板里有没有 `${`（`depth` 计数），把它作为标志返回，无插值模板按字面量处理即可。

### D.2 假红二：Python docstring 里提及保留模块（FR-5）

见 §A.3，`./zero-dispatch` 因测试文件 docstring 里的一段示例被误报成「获得了消费者」。**判定：必须修**（与 A.3 同一处修法）。

### D.3 报告质量：符号链接镜像导致错误爆炸

```text
# web/workbench/src/vendor/contracts -> ../../../../packages/contracts（symlink），再在 web/ 里相对导入
[FAILED] Contract consumer binding validation failed with 242 error(s).
```

其中 1 条是真正的逃逸（`web/workbench/src/zz-probe-link.ts:1`），其余 241 条把生成包**自己的内部相对导入**报成「Web code imports the generated package by relative path」，文件名形如 `web/workbench/src/vendor/contracts/src/admin-http.ts`——这些路径在检出上并不存在。原因是 `listConsumerFiles()` 的目录去重对 `web` 先于 `packages` 生效：先走 `web` 时经符号链接把生成包目录登记进 `seenDirectories`，随后 `packages/contracts` 被跳过。失败方向仍是 fail-closed，不构成假绿，但排障会被误导。建议：解析后落在 `packages/contracts` 内的目录直接跳过，只对**符号链接本身**报一条。

## E. `./zero-dispatch` 事实核对（本卡硬要求）

登记项现在为 `reason` + `consumer: null` + `retention`。逐条核对：

| 陈述 | 核对 | 结论 |
| --- | --- | --- |
| 「全仓没有 import `speechnexus_contracts.zero_dispatch`」 | `grep -rl … --include=*.py`（排除 `packages/contracts`）→ **0 个文件** | 真 |
| 「也没有 `@speechnexus/contracts/zero-dispatch` 的包清单导入」 | TS/JS 侧唯一提及是 `packages/contracts/tests/profiles.test.mjs:46` 读 `tests/fixtures/contracts/zero-dispatch.json` 夹具路径，不是该 exports | 真 |
| 「生成 profile 不生成客户端」 | `contracts/bindings.json` 的 `profiles.zero_dispatch.client === false` | 真 |
| `retention.basis` = `docs/components/zero-dispatch-contract-core.md` 存在且 `status: proposed` | 文件 9841 B，front-matter `status: proposed`、`version: "1.0.0"` | 真 |
| `decision` 称文档「明确固定了六个入口，并显式生成 Python `…zero_dispatch.parse` 与 TypeScript `@speechnexus/contracts/zero-dispatch` 的 `parse`、不生成客户端」 | 文档 `:33-34` 原文一致；`zero_dispatch.py` 有 `parse` 重载，`src/zero_dispatch.ts` 存在；`schemas` 恰为 6 个（`OriginalExecutionKey`、`ReceiveCancellationRequest`、`ReceiveCancellationResult`、`OriginalRecoveryQuery`、`OriginalRecoverySnapshot`、`ZeroDispatchControlError`） | 真 |
| `decision` 称「没有 HTTP 路由、生产适配器、DDL 或运行配置，默认不可启用」 | 文档 `:20-21` 原文一致 | 真 |
| `enable` 称 B 侧按「B 接续与验收边界」实现后才可能产生消费者 | 文档 `:103` 存在该小节 | 真 |
| `delete` 称「只在候选被正式放弃、并从 `bindings.json` 移除 `zero_dispatch` profile 时删除」 | 与 `bindings.json` 结构一致，可执行 | 真 |

另核上一轮点名的 `./runtime` 措辞已改准：新理由为「runtime 控制协议的 Python 消费者只有契约测试（`runtime_controller_http.py` 导入的是 `runtime_http`）」，实测 `speechnexus_contracts.runtime` 的全仓导入方只有 `tests/contract/test_runtime_http_bindings.py:11`——**准确**。其余 17 条 `reason` 被改写为结构化对象时同步措辞，机器侧已由规则 4（过期登记）兜住「TS 无消费者」这一半。

**判定：上一轮必须修 2 修好。`./zero-dispatch` 不再是被粉饰的活契约，而是一条写明「两侧都没有消费者」的候选——这是本轮最干净的成果。**

## F. 范围与例数

### F.1 返修范围（硬要求：只含本报告）

```text
$ git diff ade3da28..6451aa25 --stat
 docs/validation/ci-validation.md                |  10 +-
 packages/contracts/README.md                    |  39 +-
 packages/contracts/export-consumers.json        | 156 +++++-
 scripts/dev-env/validate-contract-consumers.mjs | 603 +++++++++++++++++++++---
 tests/ci/contract-consumers.test.mjs            | 547 +++++++++++++++------
 web/AGENTS.md                                   |   2 +-
 6 files changed, 1115 insertions(+), 242 deletions(-)

$ git diff ade3da28..6451aa25 --name-only | grep -E "^web/(workbench|admin|shared)/src|packages/contracts/src|\.schema\.json"
（无输出）
$ git diff origin/main...6451aa25 --stat | tail -1
 47 files changed, 1554 insertions(+), 65 deletions(-)
```

- 返修只碰「守卫 + 单测 + 登记表 + 两份文档 + `web/AGENTS.md` 一句话」，**没有任何 `web/` 源码、生成产物或 `packages/contracts/src/**` 改动**（本轮零改动，故上一轮「36 处导入已改经包清单」与「`web/*/dist` 与基线逐字节相同」两项结论继续成立，本轮不重做）。守卫从 341 行增至 794 行，单测从 178 行增至 457 行。
- `grep -E "generated|models\.py|parsers\.py|source-inventory"` 无输出，符合 `contracts/AGENTS.md` 的「不手改生成产物」。

### F.2 例数与 CI（本机实测）

| 检查 | 结果 |
| --- | --- |
| `node --test tests/ci/contract-consumers.test.mjs` | **29 pass / 0 fail**（自报 29，一致） |
| `node --test tests/ci/` | **68 pass / 0 fail**（自报 68/68，一致） |
| `/Users/hrygo/Documents/SpeechNexus/.venv/bin/python -m unittest discover -s tests/ci` | **Ran 370 tests … OK**（自报 370，一致） |
| `pnpm test` | EXIT 0；`web/workbench` **1063 passed / 65 files**、`web/admin` 151 passed / 19 files、`packages/contracts` 全过（自报 1063/65，一致） |
| `node scripts/dev-env/validate-contract-consumers.mjs` | `[PASSED]`，0.14–0.20 s，338 文件 |
| `uv run --no-project ruff format --check .` | EXIT 0（ruff 0.17.2；在被审树 `/private/tmp/sn828-head` 上 `1872 files already formatted`） |

`gh pr checks 824`（head 确为 `6451aa25`，`gh pr view 824` 已核 headRefOid）：

```text
$ gh pr checks 824 | wc -l  → 25 行
24 行 pass；1 行 skipping（Docs: markdown code block formatting，路径不触发）
```

即「25 项」对，「全绿」应表述为 **24 pass + 1 skipping**。CI 里守卫确实执行（`Scope routing and CI / design-token checks`，run `43670359190`）：

```text
##[group]Run node --test tests/ci/contract-consumers.test.mjs
node scripts/dev-env/validate-contract-consumers.mjs
ℹ tests 29
ℹ pass 29
Checking generated-contract consumer bindings...
[PASSED] Generated-contract exports, consumers and workspace dependencies are aligned.
```

## 必须修清单

1. **`consumer:null` + `retention` 目前是零成本豁免，必须与子路径建立机器可校验的关联。** 现状可用十行 JSON（可从 `./zero-dispatch` 整块复制）给任意新 `exports` 盖章；`basis` 指向存在但无关的文档、甚至目录都能过（R1–R4 全部 `[PASSED]`）。最小收口三项：`basis` 改为必须 `isFile()` 且正文出现该子路径字符串；每个 `binding` profile 至多一个 `retention` 项（掐死复制）；`binding` 与该子路径建立可校验对应（profile `source` 同源，或 profile 名等于子路径规范化值）。补对应负例单测。
2. **Python 侧证据检查必须遮蔽注释与字符串。** 现在 `checkConsumer()`/`findPythonImporters()` 对 `.py` 直接跑正则：docstring 里一行 `from speechnexus_contracts.ghost import parse` 即可伪造消费方（N5 `[PASSED]`），而合法文档里写同样一行会把保留候选误判成「获得了消费者」（FR-5 假红）。同一处顺带补「推导出的模块必须真的存在于 `packages/contracts/src/speechnexus_contracts/<mod>.py`」的校验（N4 用幻影模块 + 测试文件过关）。
3. **无插值的模板字面量 ``import(`…`)`` 被误判为不可静态校验（FR-2）。** `maskSource()` 已有 `${` 计数，把「模板是否含插值」带出来，无插值模板按字面量说明符处理；否则应在 README/`ci-validation.md` 明确写成「一切模板字面量一律失败」——但那是把一个合法静态写法禁掉，不推荐。

## 建议修清单（非阻塞，按性价比排序）

1. **符号链接镜像生成包会让报告爆炸**（实测 242 条，其中 241 条指向并不存在的 `web/…/vendor/contracts/src/*.ts`）。建议 `listConsumerFiles()` 在解析后落在 `packages/contracts` 内的目录直接跳过，只对符号链接本身报一条。
2. **`retention.basis` 只做 `existsSync`**，目录也放行（R3）；改成 `statSync().isFile()` 是一行的事。
3. **非 ASCII 标识符的 import 整条不可见**：`web/workbench/src/zz-probe-cjk.ts` 里 `import { 解析器 as parser } from "@speechnexus/contracts/ghost-cjk"` → `[PASSED]`（`IMPORT_CLAUSE_ALLOWED` 的 `\w` 只含 ASCII 与 `_$`）。中文别名在中文仓库里是现实写法，建议放宽字符类至 `\p{L}\p{N}`（带 `u` 标志）。
4. **模板插值 `${…}` 内的 import 被整段掩掉**：`` `${import("@speechnexus/contracts/ghost-in-tpl")}` `` → `[PASSED]`。属边缘形态，可与第 3 条同批处理。
5. **非 Python 子路径（`./schemas/*`）失去消费者后只剩 `retention` 通道**（C.2）。建议允许以 `.ts`/`.mjs` 文件作为消费方证据（要求其中出现该 `@speechnexus/contracts/<subpath>` 说明符），否则 `retention` 的实际承重比表面更大。
6. **两跳转发链仍在门禁外**（RES-1：`web/` 相对导入 `scripts/` 中转模块，由 `scripts/` 相对进入生成包 → `[PASSED]`）。前一轮已记为低严重度，本轮未收口；建议至少在文档写明「规则按文件判定，跨 `scripts/` 转发不在门禁内」。
7. **单测命名笔误**：`tests/ci/contract-consumers.test.mjs:341` 的 `a retained candidateneeds a decision document …` 缺空格。
8. **`retention` 的 `basis` 内容未被任何测试约束**：`a retained candidate passes when the binding profile generates no client` 用 `docs/candidate.md` + `"# Candidate\n"` 作为合法证据，等于把「无关文档也能当依据」固化成了契约。若采纳必须修 1，这条用例需同步收紧。

## 只能依赖作者陈述或未完全验证的点

- 「`web/*/dist` 与基线逐字节相同」「36 处导入全部改经包清单」两项**本轮未重做**（依本卡要求）。依据是本轮返修零改动 `web/` 源码与 `packages/contracts/src/**`（§F.1 的 `--name-only` 过滤为空），故上一轮（`89d56e89` 已合入）的结论继续有效；这不是本轮的实测证据。
- 未跑 `pnpm build` / `pnpm typecheck` / `pnpm test:behavior` / `pnpm test:a11y`：本轮零 Web 源码与零生成产物改动，这些检查的输入与上一轮相同；`pnpm test`（1063/65、151/19）已跑并一致。
- 未跑 `make contracts-drift`（需要完整 `uv sync` 与生成器依赖）：`packages/contracts/src/**` 零改动，drift 状态与基线相同，属「由未变的输入推断」。
- 未验证 Windows 路径分隔符与其它 Node 版本：本机 Node 26.12.1（与 `.nvmrc` 一致）、macOS arm64，CI 为 ubuntu-24.04。
- 「CI run 43670359190 25 项全绿」：我核到 25 行、24 pass + 1 skipping（`Docs: markdown code block formatting`），并把守卫在该 run 的执行日志摘出来了；除此之外不依赖作者陈述。
