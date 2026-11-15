# PR #814 非作者审查：规则文件按内容归类（fix #812）

- 被审对象：PR #814 `fix/812-docs-routing`，head `25fd3b1dc7860cfe1ffabf2b143d003db56c5dd6`，基线 `origin/main` = `086349a2`
- 审查方式：非作者审查；被审代码只读（`git show 25fd3b1d:<path>`），变异只在独立检出内施加并复原
- 检出：`/private/tmp/sn816`（本报告分支，based on `origin/main`）、`/private/tmp/sn-head`（被审 head 的只读检出，仅用于跑作者测试与施加变异）
- 审查日期：2026-11-15

## 结论

**通过，无阻塞项。** 判据方向与实现都成立，三处「字节真的进入构建/校验」的例外位置经仓库内证据逐条核对为真，没有发现某条改判文件真的影响构建或校验。以下五点需要修正，其中前两点会实际产生错误结论：

1. **自报例数错**：`15 条变化（11 条 full→docs、4 条 no-db→docs）` 与实测不符。独立复算为 **12 条变化（8 条 full→docs、4 条 no-db→docs）**；同一条 PR 描述里的逐文件表也是 12 个箭头。本审查任务卡沿用了「15」这个数，同样不成立。
2. **新守卫会误红**：`test_every_tracked_document_has_a_content_lane` 的期望集只写「Dockerfile `COPY` 的 README ∪ 两个单测读取的 engine README」，漏了路由器自己的 `web/<app>/public/**` 规则。新增一个 `web/workbench/public/README.md`（路由结果正确地是 `full`）会让该用例变红——已实测复现。
3. **Dockerfile 派生只能看见字面量 `COPY <file>.md`**：仓库里已存在目录形式的 `COPY web web`（`deploy/images/web.Dockerfile:12`）与 `COPY packages/contracts packages/contracts`（同文件 `:13`），以及将来的 `ADD`、glob `COPY` 都在派生之外。今天这些路径要么另有字面量 `COPY`、要么已按「不读内容」论证过，没有实际漏判，但「新增构建消费者会让该用例变红」这句承诺比实现强。
4. **docs 车道的「作业行数」口径与实测不符**：作者给的 21 → 21 行、18/3 与 6/15 都是推演值。实测 full（run `43658356736`）是 **20 行 = 18 success + 2 skipped**，实测 docs（本报告 PR #816，run `43660849302`）是 **15 行 = 6 pass + 9 skipping**——因为矩阵作业 skipped 时会折叠成一行。作业集合是对的，行数不是（§4.4）。
5. **docs 车道作业集的推导本身可信**，而且不是「文档描述」而是**被最终门禁强制**的：`ci.yml:847-855` 的 `case "$MODE" in docs)` 对每个作业逐一比对实际结果字符串，`docs_format` 期望 success、其余期望 skipped，任何意外执行或意外跳过都会红。合并后仍须实测一次（方案见 §4.5）。

## 判定总表

| 项 | 内容 | 判定 | 证据 |
| --- | --- | --- | --- |
| A1 | 12 条改判里是否有一条真的影响构建或校验（核心） | 通过（另纠正例数） | §1 |
| A2 | `web/workbench/src/AGENTS.md` → docs 的五条依据 | 通过（附 1 条残留风险） | §2 |
| B3 | 混合 diff 仍取更严格范围 | 通过 | §3.1 |
| B4 | 大小写 / 命名变体 | 通过（fail-closed，但变体例外未写入文档） | §3.2 |
| B5 | 嵌套 `public/` 覆盖任意深度 | 通过 | §3.3 |
| C6 | 两条反例独立复现（含 6 red） | 通过（逐字复现） | §3.4 |
| D7 | docs 车道真实作业集推导 + 合并后实测方案（核心） | 通过：推导可信、门禁强制，且有本报告 PR 的真实 docs 运行对照；行数口径不实 | §4 |
| E8 | 366 passed / 743 subtests、CI full 全绿 | 通过（本地与 CI 双向复核） | §5 |

---

## 1. 12 条改判里有没有任何一条真的影响构建或校验（本卡核心）

### 1.1 复算：32 条全量对照，改判 12 条而不是 15 条

被审 head 上 `AGENTS.md` / `README*` 共 32 条（`git ls-tree -r 25fd3b1d | grep -E '(^|/)(AGENTS\.md|README[^/]*\.md)$'`），用 `origin/main` 版与被审 head 版两个路由器逐条对比：

```text
$ python3 - <<'PY'   # 逐条 classify，base=origin/main 版，head=25fd3b1d 版
   ... 32 条全量对照，输出见下表
PY
total: 32 changed: 12
breakdown: Counter({('full', 'docs'): 8, ('no-db', 'docs'): 4})
```

所以是 **8 条 `full`→`docs` + 4 条 `no-db`→`docs` = 12 条**；`8 条构建输入不变` 这个数是对的（6 条镜像 `COPY` + 2 条单测读取）。作者描述的逐文件表标了 12 个 `<-`，与实测一致，错的只是结论段落里的 15/11。

### 1.2 逐条核对（字节消费者一律从仓库内派生，不采信陈述）

`full` 的判据不是「目录里有什么」，而是「有没有东西读它的字节」。先把仓库里全部 Dockerfile 的 `COPY`/`ADD` 打出来（全仓库只有 5 个 Dockerfile：`asset-inspector/Dockerfile`、`deploy/images/{minio,node-controller,python,web}.Dockerfile`）：

```text
asset-inspector/Dockerfile:23:COPY backend/README.md backend/pyproject.toml backend/
asset-inspector/Dockerfile:24:COPY execution-gateway/README.md execution-gateway/pyproject.toml execution-gateway/
asset-inspector/Dockerfile:25:COPY node-controller/README.md node-controller/pyproject.toml node-controller/
asset-inspector/Dockerfile:26:COPY packages/contracts/README.md packages/contracts/pyproject.toml packages/contracts/
asset-inspector/Dockerfile:27:COPY packages/media/README.md packages/media/pyproject.toml packages/media/source-inventory.json packages/media/
asset-inspector/Dockerfile:28:COPY asset-inspector/README.md asset-inspector/pyproject.toml asset-inspector/
deploy/images/node-controller.Dockerfile:13:COPY backend/README.md backend/pyproject.toml backend/
deploy/images/node-controller.Dockerfile:14:COPY execution-gateway/README.md execution-gateway/pyproject.toml execution-gateway/
deploy/images/node-controller.Dockerfile:16:COPY packages/media/README.md packages/media/pyproject.toml packages/media/
deploy/images/node-controller.Dockerfile:17:COPY node-controller/README.md node-controller/pyproject.toml node-controller/
deploy/images/python.Dockerfile:12:COPY backend/README.md backend/pyproject.toml backend/
deploy/images/python.Dockerfile:13:COPY execution-gateway/README.md execution-gateway/pyproject.toml execution-gateway/
deploy/images/python.Dockerfile:14:COPY node-controller/README.md node-controller/pyproject.toml node-controller/
deploy/images/python.Dockerfile:16:COPY packages/media/README.md packages/media/pyproject.toml packages/media/source-inventory.json packages/media/
deploy/images/python.Dockerfile:17:COPY asset-inspector/README.md asset-inspector/pyproject.toml asset-inspector/
deploy/images/web.Dockerfile:12:COPY web web
deploy/images/web.Dockerfile:13:COPY packages/contracts packages/contracts
deploy/images/web.Dockerfile:27:COPY --from=build /workspace/web-dist /usr/share/nginx/html
```

**没有任何 `COPY deploy/`、没有任何 `COPY contracts/`**（`web.Dockerfile:13` 复制的是 `packages/contracts`，与根 `contracts/` 是不同路径）。镜像工作流的 scope 也把六条 README 逐一列成构建输入（`asset-inspector-image.yml:25,27,29,31`、`node-controller-image.yml:52-54`），与 `_IMAGE_COPY_PROSE` 双向一致。

逐条判定（32 条全量；「消费者」列是仓库内可跑的读取点）：

| 路径 | 改前 | 改后 | 字节消费者 |
| --- | --- | --- | --- |
| `AGENTS.md` | docs | docs | 无（agent 规则文件） |
| `README.md` | docs | docs | 根 `pyproject.toml:5 readme=`，但 `[tool.uv] package = false` → 根包不构建 |
| `README.en.md` | docs | docs | 无 |
| `asset-inspector/README.md` | full | full | `asset-inspector/Dockerfile:28`；`asset-inspector-image.yml` 的 `asset-inspector/**` |
| `backend/README.md` | full | full | `python.Dockerfile:12`、`node-controller.Dockerfile:13`；`pyproject.toml:5` |
| `contracts/AGENTS.md` | full | **docs** | 无 |
| `contracts/README.md` | full | **docs** | 无（`validate-contracts.py:107-116` 只取 `*openapi.yaml` / `*.schema.json`；`tests/contract/test_p0_public_contracts.py:1941,1950,1982` 的 `contracts.glob("**/*")` 全部按后缀过滤 `.json/.yaml/.yml`） |
| `deploy/README.md` | full | **docs** | `tests/ci`：`test_reference_deploy_job_prereqs.py:50,295`、`test_reference_deploy_profile_publication.py:25,54`——**docs 车道的 `changes` 作业照样执行**（见 §1.3） |
| `deploy/engines/cpu-vad/README.md` | no-db | **docs** | 无 |
| `deploy/engines/file-asr-audio/README.md` | no-db | **docs** | 无（`artifact-pin.json` 的 `model_card.file_name = README.md` 指 HF 仓库文件，`repository_files` 只钉 `requirements.txt`/`runtime-pin.json`/`SILERO_LICENSE`） |
| `deploy/engines/file-asr-audio/evidence/gb10-20261027/README.md` | no-db | **docs** | 无（它记录同目录兄弟文件的 SHA-256，本身不被任何脚本读取） |
| `deploy/engines/file-tts-customvoice/README.md` | full | **docs** | 无（`artifact-pin.json` 同上是 HF 侧文件名） |
| `deploy/engines/file-voice-clone/README.md` | full | full | `tests/unit/gateway/test_engine_language_semantics.py:24-30` 的 `SEED_DOCUMENTS` |
| `deploy/engines/file-voice-design/README.md` | full | full | 同上 |
| `deploy/engines/vllm-asr-lifecycle/README.md` | no-db | **docs** | 无 |
| `deploy/private/README.md` | full | **docs** | 无（`reference_acceptance` 只从 `deploy/compose.yaml` 派生 `<name>.example.json`，不读 README） |
| `docs/README.md` 及 `docs/*/README.md` 共 9 条 | docs | docs | 无 |
| `execution-gateway/README.md` | full | full | `python.Dockerfile:13`、`node-controller.Dockerfile:14`；`pyproject.toml:5` |
| `node-controller/README.md` | full | full | `python.Dockerfile:14`、`node-controller.Dockerfile:17`；`pyproject.toml:5` |
| `packages/contracts/README.md` | full | full | `asset-inspector/Dockerfile:26`；`web.Dockerfile:13` 目录复制 |
| `packages/media/README.md` | full | full | `python.Dockerfile:16`、`node-controller.Dockerfile:16`；`pyproject.toml:5` |
| `tests/integration/AGENTS.md` | full | **docs** | 无（`tests/integration/test_pg_dsn_skip_guard.py:3` 只在 docstring 里提到路径） |
| `tests/integration/README.md` | full | **docs** | 无 |
| `web/AGENTS.md` | full | **docs** | 无（见 §1.4） |

**结论：12 条改判里没有一条的字节被 docs 车道跳过的检查读取。** 四个 `no-db`→`docs` 的 engine README 原本是 `no-db` 是「位置副作用」（`deploy/engines/<白名单包>/` 下任何文件都吃 `no-db`），不是因为这些 README 是 engine 构建或测试输入——仓库里**没有任何 engine 的 Dockerfile**，engine 镜像不在本仓库构建。

### 1.3 `deploy/README.md` 走 docs 的关键前提成立

`deploy/README.md` 是唯一被代码读取的改判文件，所以必须核「docs 车道跑不跑那个读取者」。`ci.yml:35-36`：

```text
- name: Verify routing boundaries without installing project dependencies
  run: python3 -m unittest discover -s tests/ci -v
```

这个步骤在 `changes` 作业里、**没有 `if:`**，因此 docs/web/no-db/full 四种范围都执行；`changes` 失败会让 `Gate: required checks for this change` 因 `ROUTING_RESULT != success` 变红（`ci.yml:811-844`）。所以 `DeployReadmeGateTests` 与 `ProfilePublicationAuthorityTests` 在 docs 车道上照常运行，作者这条论证成立。

### 1.4 需要记录的细节：`web/AGENTS.md` 的字节确实进了一个镜像层，但不进产物

`deploy/images/web.Dockerfile:12` 是目录复制 `COPY web web`，`.dockerignore` 也没有排除 `*.md`/`AGENTS.md`，所以 `web/AGENTS.md`（以及源码树里的规则文件）**会进入 builder 阶段的层**。最终阶段只 `COPY --from=build /workspace/web-dist`（`:27`），Vite 不读未被导入的 `.md`，因此不影响产物内容——作者的结论对，但 `docs/validation/ci-validation.md` 新增段里「其余说明文件没有构建或检查读取」这句比事实强：准确表述是「Vite/tsc/ESLint/Prettier 都不读这些文件，最终镜像只发布 `dist`」。这一处建议改成事实化措辞（低风险，非阻塞）。

## 2. `web/workbench/src/AGENTS.md` → docs 的五条依据逐条核实

该文件在当前 head 上不存在（32 条里没有它），属「将来会出现的位置」；作者的依据是「没有消费者」，逐条核实：

| 作者依据 | 实测 | 结论 |
| --- | --- | --- |
| 全仓库没有对 `.md` 的 `?raw` / `import.meta.glob` 导入 | 在 25fd3b1d 上对 `web/` 搜 `?raw` / `?url` / `import.meta.glob` → 无命中；搜源码里出现的 `.md` 字面量 → 无命中 | 成立 |
| Vite 只跟随导入，`publicDir` 只对 `public/` 生效 | `web/{admin,workbench}/vite.config.ts` 都没有覆盖 `publicDir` / `assetsInclude` / `fs.allow`（默认 `publicDir = public`，默认 `assetsInclude` 不含 `.md`） | 成立 |
| `tsc` 只编译 TS/TSX | `web/workbench/tsconfig.json` 的 `include: ["src", "vite.config.ts"]`，`tsc` 对目录只收 `.ts/.tsx/.d.ts` | 成立 |
| ESLint glob 只含 ts/tsx/mjs | `eslint.config.js:7-8`：`browserFiles = ["web/**/*.{ts,tsx}", "packages/contracts/src/*.{ts,mts}"]`（作者写的「mjs」略不精确，但 `.md` 确实不在内） | 成立 |
| Prettier glob 不含 `*.md` | `package.json` 的 `format:check` 只覆盖 `web/**/*.{css,html,json,ts,tsx}`、根配置、`.github/**/*.{yaml,yml}`、`deploy/**/*.yaml`、`packages/contracts/**/*.{ts,mts,mjs,json}`、`tests/{ci,web}/*.mjs` | 成立 |
| 最终只发布 `dist` | 见 §1.4：最终阶段成立，builder 阶段会带上 | 结论成立、表述需精确 |

**残留风险（建议项，不是阻塞）**：若将来某次改动通过 `?raw` 导入某个 `.md`，引入导入的那次 PR 会命中 `web` 车道并构建（会红/会绿都是真实信号），但**之后再改那个 `.md` 仍是 `docs`，不会再构建**，而现存守卫没有任何一条能发现这件事（`dockerfile_readme_copies()` 只看 Dockerfile，不看 `import`）。建议补一条「`web/` 下不存在 `.md` 导入」的回归，把这条人工结论变成可检测项。

## 3. 判据绕过点实测

### 3.1 混合 diff 仍取更严格范围（B3）

```text
['web/AGENTS.md', 'web/workbench/src/App.tsx']                    base=full  head=web      # 非 docs ✓
['web/workbench/src/App.tsx', 'web/AGENTS.md']                    base=full  head=web
['web/AGENTS.md', 'backend/src/speechnexus_backend/job_runtime.py'] base=full head=full     ✓
['web/AGENTS.md', 'deploy/engines/cpu-vad/requirements.in']       base=full  head=no-db
['web/AGENTS.md', 'scripts/dev-env/ci-routing.py']                base=full  head=full     ✓
['web/AGENTS.md', 'uv.lock']                                      base=full  head=full     ✓
```

要求是「必须不是 `docs`」：满足。`web` / `no-db` 两条相对 base 从 `full` 收紧，但收紧到的正是**代码单独改动本来就该拿到的车道**，所以不是「文档掩盖代码」，与本仓库「任意混合变更取更严格范围」的口径一致（严格范围 = 各路径车道的最大值，`docs` 最小）。

`classify` 的实现保证了这一点：`prose_route == "docs"` 只 `continue`，`prose_route == "full"` 立刻 `return "full"`，非规则的未知路径仍落到 `return "full"`。

### 3.2 大小写与命名变体（B4）

```text
web/agents.md        -> full      web/agEnTs.md     -> full
web/AGENTS.MD        -> full      web/Readme.md     -> full
web/readme.md        -> full      web/README        -> full
web/README.rst       -> full      web/README.en.txt -> full
README.en.md         -> docs      web/README.zh-CN.md -> docs   (README(?:\.[^/]+)?\.md)
```

方向是 fail-closed（变体拿更重的车道，不会漏跑验证），但意味着 **#812 抱怨的「同一类文件两种待遇」对命名变体仍然存在**：`web/agents.md` 与 `web/AGENTS.md` 仍是 `full` vs `docs`。这不是安全漏洞，但文档里目前没有点名这个例外（现在只写了「`CONTRIBUTING.md`、`.github/*.md` 等非 `AGENTS.md`/`README*` 的 Markdown 仍走 full」）。建议补一句命名变体说明，避免下一个人把 `agents.md` 当成路由 bug 再开一张卡。

### 3.3 嵌套 `public/` 覆盖任意深度（B5）

```text
web/workbench/public/AGENTS.md            -> full
web/workbench/public/notes/README.md      -> full
web/admin/public/README.md                -> full
web/shared/public/README.md               -> full
web/newapp/public/README.md               -> full   # 新应用也覆盖
web/workbench/src/public/README.md        -> docs   # 正确：Vite publicDir 只认应用根下 public
web/workbench/public/README.md            -> full
```

判据是 `len(parts) >= 4 and parts[0] == "web" and parts[2] == "public"`，因此 `public/` 下**任意深度**都命中（`parts[2]` 只看第三段，深度无关），且不写死应用名。`src/public/` 不命中是正确的（默认 `publicDir` 是根下 `public`）。

### 3.4 反例复现（C6，至少两条，含那条 6 red）

反例 1：临时 git 仓库里只有 `web/AGENTS.md` 一个改动，用真实 CLI 跑两个版本的脚本：

```text
$ python3 ci-routing-base.py --event=pull_request --base=$BASE
{"mode": "full", "reason": "Shared, unknown, executable or empty changes require every verification lane."}
$ python3 ci-routing-head.py --event=pull_request --base=$BASE
{"mode": "docs", "reason": "Only prose or skill documents/metadata changed; heavy checks are out of scope."}
$ git diff --name-only $BASE..HEAD
web/AGENTS.md
```

与作者报告逐字一致。

反例 2（6 red）：在被审 head 的独立检出里把判据改回位置判断（`_prose_route` 里加 `if parts[0] == "web": return "full"`），跑 docs 车道真实执行的命令：

```text
$ python3 -m unittest discover -s tests/ci -p 'test_ci_routing.py'
FAIL: test_a_document_never_hides_a_code_change ... AssertionError: 'full' != 'web'
FAIL: test_every_tracked_document_has_a_content_lane ... Items in the first set but not the second
SUBFAILED(path='web/AGENTS.md') test_the_same_document_gets_the_same_lane_at_every_level 'full' != 'docs'
SUBFAILED(path='web/workbench/src/AGENTS.md') ... 'full' != 'docs'
SUBFAILED(path='web/shared/src/README.md') ... 'full' != 'docs'
FAIL: test_web_agents_is_no_longer_full ... AssertionError: 'full' != 'docs'
Ran 35 tests ... FAILED (failures=6)
$ git checkout -- scripts/dev-env/ci-routing.py && python3 -m unittest discover -s tests/ci -p 'test_ci_routing.py'
Ran 35 tests ... OK
```

与作者报告的 6 条失败名逐条一致（还原后全绿）。

反例 3（安全边界未收窄）：`web/workbench/public/AGENTS.md`、`web/admin/public/README.md`、`backend/README.md`、`deploy/engines/file-voice-design/README.md` 全部仍为 `full`（见 §3.3 与 §1.2）。

### 3.5 一个新发现：守卫会误红（已实测）

`tests/ci/test_ci_routing.py:237` 断言「判为 `full` 的规则文件集合 == Dockerfile 复制集 ∪ 单测读取集」。这个期望集**漏掉了路由器自己的 `web/<app>/public/**` 规则**。在 head 上新建一个 `web/workbench/public/README.md`（路由结果正确为 `full`）：

```text
classify(['web/workbench/public/README.md']) = full
$ python3 -m unittest ...ProseRoutingTests.test_every_tracked_document_has_a_content_lane
AssertionError: Items in the first set but not the second:
'web/workbench/public/README.md'
FAILED (failures=1)
```

即：**正确的新增会红，而失败信息是「a rule file/README took a source lane instead of docs or full」，与真实原因无关。** 这是本 PR 唯一会实际误伤后续改动的缺陷。修法见 §6。

## 4. docs 车道的真实作业集：推导是否可信 + 合并后怎么实测（本卡核心）

### 4.1 从 `ci.yml` 的 `if:` 推导 docs 模式

本 PR 未改工作流（`git diff --stat origin/main 25fd3b1d -- .github/workflows/` 为空），所以作业条件就是 `main` 的现状：

| 作业 | `if:` | docs 模式 |
| --- | --- | --- |
| `changes`（Scope routing and CI / design-token checks） | 无（`:20-27`） | **执行**（路由回归 `tests/ci`、Design Token、审查 PR 边界） |
| `docs_format`（Docs: markdown code block formatting） | `mode == 'docs'`（`:95`） | **执行** |
| `delivery_tracking`（Docs: delivery matrix freshness） | 无（`:122-127`） | **执行**（重渲染步骤按路径跳过，作业 success） |
| `python_checks` | `full\|web\|no-db`（`:178`） | 跳过 |
| `python_contracts` | `full\|web\|no-db`（`:270`） | 跳过 |
| `integration_shards` ×6（Integration 0–5） | `full\|web`（`:375`） | 跳过 |
| `coordination` | `always() && success && full`（`:600`） | 跳过 |
| `python_checks_gate`（Gate: Python execution and static checks） | `full\|web\|no-db` | 跳过 |
| `node_controller_image`（Node Controller image evidence） | `always() && changes.success` | **执行**：内部 `scope` 与 `gate` 跑，`image` 两架构因 `required != true` 跳过 |
| `python`（Gate: required checks for this change） | `always()` | **执行** |
| `web` | `full\|web\|no-db` | 跳过 |
| `deployment`（Compose: configuration validation） | `full\|no-db` | 跳过 |
| `reference_acceptance`（Deploy: reference acceptance entry） | `full\|no-db` | 跳过 |

**实际执行 = 5 个作业 / 6 行**：`changes`、`docs_format`、`delivery_tracking`、`node_controller_image` 的 `scope` 与 `gate` 两行、最终门禁 `python`。作者报的「改后执行 6 项」与这里的 6 行一致（他们把这 6 行按 5 条列出，把 node-controller 的 `scope`+`gate` 合写成一条）。

### 4.2 推导为什么可信：它被门禁强制，不只是文档

`ci.yml:811-855` 的最终门禁不只是「检查关键作业」：

```text
case "$MODE" in
  docs)
    checks=skipped; coordination=skipped; integration=skipped;
    web=skipped; deployment=skipped; acceptance=skipped;
    docs_format=success ;;
  ...
esac
if [[ "$CHECKS_RESULT" != "$checks" || "$COORDINATION_RESULT" != "$coordination" || ... ]]
```

它对**每个作业的实际结果字符串**逐一比对「应当是什么」，并且要求 routing、`delivery_tracking`、`node_controller_image` 均 success、controller scope 输出只能是 `true|false`。因此：

- 若 docs 模式下某个本该跳过的作业真的执行了（哪怕成功），门禁照样红（`skipped` ≠ `success`）；
- 若 `docs_format` 被意外跳过，门禁红（`success` ≠ `skipped`）。

也就是说「docs 模式的作业集」不是作者凭 `if:` 猜出来的软结论，而是**门禁会强制**的硬约束；作者把它作为推导值交付可以接受。这是本项可以判「可信」的主要理由，而不是「作者的推导逻辑看起来对」。

### 4.3 这条推导有一次真实的 docs 模式运行可以对照

本审查自身就是一个 docs 范围 PR（本报告 PR，run `43660849302`），因此它的运行就是一次 docs 车道的实际观测：

```text
$ gh run view 43660849302 --log | grep -F '"mode"'
{"mode": "docs", "reason": "Only prose or skill documents/metadata changed; heavy checks are out of scope."}
$ gh pr checks 816 | awk -F'\t' '{print $2}' | sort | uniq -c
   6 pass
   9 skipping
$ gh pr checks 816      # 实际执行的行
Scope routing and CI / design-token checks                       pass
Docs: markdown code block formatting                             pass
Docs: delivery matrix freshness                                  pass
Node Controller image evidence / scope                           pass
Node Controller image evidence / Gate: node-controller evidence   pass
Gate: required checks for this change                            pass
```

执行集合与 §4.1 的推导**逐条一致**，全部 6 项 success、最终门禁 pass。**限定**：这次 docs 路由来自基线版路由器（规则是 `docs/` 内 Markdown，不是 #812 的新判据），所以它证明的是「docs 模式的实际作业集 = 推导集合」，不是「`web/AGENTS.md` 在新判据下判 docs」——后者由 `tests/ci` 的新用例与本报告 §3.4 的真实 CLI 复现证明，仍须按 §4.5 在合并后补最后一步。

### 4.4 作业「行数」不是稳定量（必须修）

同一份工作流在两个范围内渲染出的行数不同，因为它们随**矩阵作业 skipped 时是否展开**变化：

```text
# full（本 PR 运行 43658356736）：20 行 = 18 success + 2 skipped
$ gh run view 43658356736 --json jobs --jq '.jobs[] | "\(.conclusion)\t\(.name)"' | sort | uniq -c
  1 skipped  Docs: markdown code block formatting
  1 skipped  Node Controller image evidence / Build and probe (${{ matrix.runner }})
 18 success  （其余）

# docs（本报告 PR #816 运行 43660849302）：15 行 = 6 pass + 9 skipping
# Integration 0–5 六片全 skipped 时折叠成 1 行，node-controller 镜像两架构同样折叠成 1 行
```

所以作者写的「21 行 → 21 行、18/3 与 6/15」既不是 full 的实测值（20/18/2）也不是 docs 的实测值（15/6/9）。**作业集合正确，行数错误**；报告与文档应改用「作业集合」口径。

### 4.5 合并后如何实测（具体可执行）

本 PR 无法自证 docs 车道的真实运行（任何含路由脚本的 PR 都按 `full` 比较，这一点作者已声明）。合并后按下面步骤实测一次，成本约 1 次 docs 模式的 CI 运行（实测应只有 6 行会真跑，其余 skipped，通常 1–2 分钟）：

```sh
# 1) 从合并后的 main 拉分支，只动一个规则文件（内容无需有意义，只要一个可检出的改动）
git switch -c docs/verify-812-route origin/main
printf '\n<!-- 路由实测：合并后由 PR #814 的判据决定车道 -->\n' >> web/AGENTS.md
git commit -am "docs: 验证 #812 判据在合并后的真实车道" && git push -u origin docs/verify-812-route
gh pr create --title "docs: 验证 #812 判据实测（临时）" --body "合并后实测 docs 车道作业集，验证后关闭，不合并。"

# 2) 核对路由输出（changes 作业日志里必须出现 mode=docs）
gh pr checks <pr> --watch
RUN=$(gh pr view <pr> --json statusCheckRollup --jq '.statusCheckRollup[0].detailsUrl' | grep -o '/runs/[0-9]*' | cut -d/ -f3)
gh run view "$RUN" --json jobs --jq '.jobs[] | "\(.conclusion)\t\(.name)"' | sort

# 3) 断言：执行集合必须恰好是这 6 行，其余全部 skipped
#    Scope routing and CI / design-token checks
#    Docs: markdown code block formatting
#    Docs: delivery matrix freshness
#    Node Controller image evidence / scope
#    Node Controller image evidence / Gate: node-controller image evidence
#    Gate: required checks for this change
gh run view "$RUN" --log --job "$(gh run view "$RUN" --json jobs --jq '.jobs[]|select(.name|startswith("Scope routing"))|.databaseId')" | grep -F '"mode": "docs"'

# 4) 关闭临时 PR（不合并、删分支），main 的 push 运行仍按设计全量
gh pr close <pr> --delete-branch
```

判定标准：① `changes` 日志出现 `{"mode": "docs", ...}` 且 `excluded_shards=[]`；② 非 skipped 的行恰好是上面 6 行；③ 最终门禁 success。三项都成立才算「docs 车道作业集已被实测」，任一项不符即说明 `if:` 推导或门禁映射有漏。

**另一条更强的可选验证**（成本更高，仅当怀疑 docs_format 覆盖不足时做）：上面第 1 步改成在 `web/AGENTS.md` 里故意放一个未格式化的 `python` 代码块，预期 `docs_format` 变红——我在本机已验证 `ruff 0.17.2` 确实解析 Markdown 内的 Python 代码块（`ruff format --check` 对 `R.md` 报 `unformatted: File would be reformatted`，退出码 1），所以这条路径是真实的，不是空转。

## 5. 例数与 CI（E8）

本地实测（被审 head 的独立检出，`.venv` 复用本机已同步环境）：

```text
$ uv run --no-sync pytest tests/ci -q
366 passed, 743 subtests passed in 10.23s
$ python3 -m unittest discover -s tests/ci        # docs 车道实际执行的命令
Ran 366 tests in 10.146s
OK
$ uv run --no-sync ruff format --check .
1869 files already formatted
$ uv run --no-sync ruff check .
All checks passed!
$ uv run --no-sync mypy --platform linux
Success: no issues found in 1457 source files
```

`gh pr checks 814`（run `43658356736`）20 行全部通过，仅 2 行 skipping（`Docs: markdown code block formatting`、node-controller 镜像构建），无 failure/cancel。作者关于「full 全绿」的陈述成立；`lint-imports` 本机未能复现（借用的 venv 未安装 workspace 包，报 `Could not find package 'speechnexus_asset_inspector'`），但该步骤在本次 full 运行的 `Python: lint, types and contract compatibility` 里实际执行并通过，因此不影响结论。

例数结构也自洽：被审 head 的 `test_ci_routing.py` 单独跑是 `Ran 35 tests` = `origin/main` 版的 28 个用例 + `ProseRoutingTests` 新增 7 个（`test_web_agents_is_no_longer_full`、`test_the_same_document_gets_the_same_lane_at_every_level`、`test_a_document_never_hides_a_code_change`、`test_every_tracked_document_has_a_content_lane`、`test_readmes_copied_into_an_image_keep_the_full_lane`、`test_prose_in_a_web_public_directory_stays_full`、`test_readmes_a_unit_test_asserts_keep_the_full_lane`），`GitRoutingTests` 等既有用例未改。

## 6. 必须修清单

| # | 项 | 位置 | 修法 | 严重度 |
| --- | --- | --- | --- | --- |
| 1 | 自报例数错：`15 条变化（11 + 4）` 与实测 12 条（8 + 4）不符 | PR 描述「全量路径对照」结论段 | 改成 12（8 条 full→docs、4 条 no-db→docs），与本段表格的 12 个箭头一致 | 必须（证据准确性） |
| 2 | `test_every_tracked_document_has_a_content_lane` 期望集漏 `web/<app>/public/**` 规则，新增合法的 public README 会误红（已实测） | `tests/ci/test_ci_routing.py:237-246` | 期望集改为 `dockerfile_readme_copies() \| ROUTER._TEST_READ_PROSE \| {p for p in prose_paths() if ROUTER._prose_route(PurePosixPath(p)) == "full" and <public 规则>}`，或直接断言「每条 tracked 规则文件的 `classify` 结果等于 `_prose_route`」并与派生集合双向核对 | 必须（会误伤后续改动） |
| 3 | docs 车道「作业行数」口径与实测不符（full 实测 20 行 = 18+2，而非 21 = 18+3；docs 实测渲染 15 行 = 6 pass + 9 skipping，按作业计 6 执行 + 14 跳过，而非 6+15） | PR 描述「作业名单对照」表；如需同步 `docs/validation/ci-validation.md` 的相应表述 | 改成「作业集合」口径并列出作业名；行数若保留需标注「矩阵 skipped 时不展开」 | 必须（数据准确性） |
| 4 | `docs/validation/ci-validation.md` 新增段「其余说明文件没有构建或检查读取」比事实强（`COPY web web` 会把 `web/AGENTS.md` 带进 builder 层） | `docs/validation/ci-validation.md` 新增段落 | 改为事实化表述：「Vite/tsc/ESLint/Prettier 都不读这些文件，最终镜像只发布 `dist`」 | 建议 |
| 5 | 「新增构建消费者会让该用例变红」的承诺强于实现：派生只认字面量 `COPY <file>.md`，看不见目录复制（现存 `COPY web web`、`COPY packages/contracts packages/contracts`）、`ADD` 与 glob | `scripts/dev-env/ci-routing.py:40-43` 注释、`tests/ci/test_ci_routing.py:160-174` | 要么把派生扩展到目录形式 COPY（对每个目录形式的源做 `tracked_paths()` 前缀匹配），要么在注释与文档里写明「目录复制 / ADD / glob 不在派生内，需人工登记」 | 建议 |
| 6 | 命名变体例外无文档（`agents.md`、`README.rst`、`README` 等仍走 `full`，与 #812 的抱怨同类） | `docs/validation/ci-validation.md` 新增段 | 补一句：仅 `AGENTS.md`/`README*.md` 按内容归类，其余命名仍走 `full`（fail-closed） | 建议 |
| 7 | `web/` 源码树内 `.md` 若被 `?raw` 导入，后续单独改该文件仍是 `docs` 且不再构建，无守卫可发现 | 路由脚本注释或 `tests/ci` | 加一条回归「`web/` 内不存在 `.md` 导入」，把当前的人工结论变成可检测项 | 建议 |

前 3 条修完即可合并；4-7 建议同批处理，其中 5 与 7 是把「今天的正确」变成「将来的可检测」。

## 7. 只能依赖作者陈述的点

1. **docs 车道的真实运行**：本 PR 自身必然按 `full` 比较（改动含 `scripts/dev-env/` 与 `tests/ci/`），因此「改后只跑 6 行」无法在本 PR 内实跑。我按 `ci.yml` 的 `if:` 与门禁 `case` 推导（§4.1），并用「门禁精确比对每个作业结果」证明该集合受强制（§4.2）；已用本报告 PR 的 docs 运行对照过作业集（§4.3），仍须按 §4.5 在合并后补一次「`web/AGENTS.md` 路径」的终验。
2. **`lint-imports`（9 kept, 0 broken）**：本机环境无法复现（借用的 venv 缺 workspace 包）；以本次 full 运行里该步骤通过为准。
3. **作者「`uv run ruff format --check .` 1869 files」与我实测 1869 一致**、`mypy 1457` 一致；这两项已独立复核，不属于只能依赖作者。
4. **`docs/engines` 侧的历史口径**（例如四个 engine README 以前为什么落在 `no-db`）没有设计文档可查，我只能核到「白名单规则 + 当前无消费者」，不能证明历史上没有别的消费路径。

## 附录：实跑命令与关键输出

```sh
# 独立检出（本报告分支，必须基于 origin/main）
git fetch && git worktree add /private/tmp/sn816 -b review/816-docs-routing origin/main
# 被审内容只读：不检出被审 head 的工作树，用 git show 取字节
git show 25fd3b1d:scripts/dev-env/ci-routing.py   # 判据
git show 25fd3b1d:tests/ci/test_ci_routing.py     # 新回归
git show 25fd3b1d:docs/validation/ci-validation.md # 文档同步

# 32 条全量对照（base=origin/main 版 vs head=25fd3b1d 版，独立 classify）
python3 /tmp/probe_route.py   # total 32 / changed 12 / Counter({('full','docs'): 8, ('no-db','docs'): 4})

# 字节消费者（只读 grep，不改产品代码）
git ls-tree -r --name-only 25fd3b1d | grep -iE 'Dockerfile$' | while read f; do git show "25fd3b1d:$f" | grep -nE '^\s*(COPY|ADD)'; done
git grep -nE "r?glob\((\"|')[^(\"']*\.md" 25fd3b1d -- '*.py'
git grep -n -F "deploy/README.md" 25fd3b1d -- tests/

# 被审 head 的只读检出（跑作者测试 / 施加变异，事后复原）
git worktree add --detach /private/tmp/sn-head 25fd3b1d
cd /private/tmp/sn-head && uv run --no-sync pytest tests/ci -q          # 366 passed, 743 subtests
cd /private/tmp/sn-head && python3 -m unittest discover -s tests/ci     # Ran 366 tests OK
cd /private/tmp/sn-head && uv run --no-sync ruff format --check . | ruff check . | mypy --platform linux
# 变异：_prose_route 改回位置判断 → Ran 35 tests ... FAILED (failures=6) → git checkout 复原 → OK
# 误红复现：新建 web/workbench/public/README.md → test_every_tracked_document_has_a_content_lane 红（复原后绿）

gh pr checks 814                                              # 20 行：18 pass / 2 skipping
gh run view 43658356736 --json jobs --jq '.jobs[]|"\(.conclusion)\t\(.name)"'  # full 的 20 行
gh pr checks 816 | awk -F'\t' '{print $2}' | sort | uniq -c           # docs 的 15 行：6 pass / 9 skipping
gh run view 43660849302 --log | grep -F '"mode"'                     # {"mode": "docs", ...}
```

时间线（2026-11-15，Asia/Shanghai）：

| 时间 | 动作 |
| --- | --- |
| 08:44 | `git fetch`、`worktree add /private/tmp/sn816 -b review/816-docs-routing origin/main`（`086349a2`）；确认 `docs/reviews/` 下无 `2026-11-15-docs-routing.md` |
| 08:45 | 读 `docs/validation/ci-validation.md`（542 行）与 `.github/workflows/ci.yml`（1235 行）；确认本 PR 未改工作流 |
| 08:47 | 32 条规则文件全量对照 → 改判 12 条（作者报 15 条），记录差异 |
| 08:49 | 全仓库 Dockerfile `COPY` 清单、`deploy/engines` 消费者、`deploy/README.md` 的 `tests/ci` 读者逐条核对 |
| 08:51 | 建立 `/private/tmp/sn-head`（head 只读检出）；`pytest tests/ci` 366/743、`unittest` 366 OK |
| 08:53 | B3/B4/B5 判据矩阵实测；C6 反例 1（真实 CLI）、反例 2（6 red 变异并复原） |
| 08:56 | `ruff format`（1869）、`ruff check`、`mypy`（1457）；`gh pr checks 814`、`gh run view` 行数核对 |
| 08:58 | 发现并复现守卫误红（新增 `web/workbench/public/README.md`） |
| 09:02 | 写本报告；`uv run --no-project ruff format --check` 通过后提交 |
| 09:05 | 开本报告 PR #816；确认 `git diff origin/main...HEAD --stat` 只有报告文件 |
| 09:06 | PR #816 的 CI 完成：`{"mode": "docs"}`、6 pass / 9 skipping、最终门禁 pass（run `43660849302`）——把这次真实 docs 运行补进 §4.3 并据此把行数口径问题写成实测结论 |
