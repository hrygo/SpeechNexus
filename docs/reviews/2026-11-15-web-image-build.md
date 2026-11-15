---
title: PR #836 非作者审查：Web 镜像构建上下文缺口（#834）
status: active
---

# PR #836（`fix/834-web-image-build`）非作者审查记录

被审对象：PR #836《fix(deploy): web 镜像构建补齐或被切断的测试 fixture 依赖 (#834)》，
head `9cc3c461b79ed8afdc271da89add178735e5baab`，单 commit，base `origin/main`
（`6c886c86`），diff 13 文件 `+1024 / −18`：`deploy/images/web.Dockerfile`、
`deploy/README.md`、`docs/validation/ci-validation.md`、`.github/workflows/ci.yml`、
根 `package.json`、`web/{admin,workbench}/{package.json,src/AppEntry.tsx,tsconfig.build.json}`、
新增 `scripts/dev-env/verify-web-image-context.py`（606 行）与
`tests/ci/test_web_image_context.py`（274 行）。

审查方式：`/private/tmp/sn837` 是**基于 `origin/main` 拉的审查分支**
`review/837-web-image-build`，只放本报告（写报告前 `git diff origin/main...HEAD --stat`
为空）；`/private/tmp/sn837-head` 是**同仓库 detached worktree，签出于被审 head
`9cc3c461`**，所有 `pnpm`／`pytest`／浏览器实跑都在树上做；被审内容另用
`git show 9cc3c461:<path>` 与 `git diff origin/main...9cc3c461` 读取。基线
`origin/main` 的镜像构建复现另建 `/tmp/r837/basefull`（`git archive origin/main`）。
变异全部在 `/tmp/r837/{mut,mut2,nonpmrc,base,head}` 等**仓库外副本**上施加、逐条还原，
**未改动仓库内任何产品代码/测试/脚本/部署文件，未 push 任何变异**。
`docs/reviews/` 在 `origin/main` 上已有 59 份报告，文件名
`2026-11-15-web-image-build.md` 在 `origin/main` 与 `9cc3c461` 上均不存在，未覆盖任何既有文件。

环境：macOS arm64，node `v26.12.1`（mise）、pnpm `12.14.0`、
Python `3.14.9`（主仓 `.venv`，pytest 9.1.1）、ruff `0.17.2`、
Docker `29.9.3`（BuildKit，desktop-linux）。核验日期 2026-11-15。

## 结论

**可以合并：作者自报的技术核心成立，未发现必须修项。**

关键的「红在 tsc 而不是 vite build」独立复现到字节级：隔离上下文里
`tsc -p tsconfig.json` **退出 2**，`vite build` **退出 0**，且加载/不加载
`tests/fixtures/contracts` 的产物是**同一个** `index-CzHTtsvb.js`（754.89 kB）；
被审 head 的 `${APP_NAME}` 两个镜像在真实 `docker build` 下均 **EXIT=0**，而
`origin/main` 的同一条命令 **EXIT=1**（`#834` 复现）。镜像内**没有任何测试数据**，
产物与我从 COPY 集合单独构建的产物**逐字节相同**。新增门禁的 `--build` 半场确实在
CI 里跑，静态半场的盲区也按要求能独立复现。

问题集中在**文档口径与文案精度**：静态半场不解析 bare specifier 这件事没写进
`docs/validation/ci-validation.md` 的边界段；`.npmrc` 类检查的失败文案把「策略分叉」
说成「构建会失败」。二者都不是阻断项，见「建议修」。

## 本卡核心四问

### 1. 失败点是否真在 tsc —— 是，且只在 tsc

独立物化的基线上下文**只含 `origin/main` Dockerfile 的 COPY 集合**
（426 文件，`tests/` 不存在），在其中直接跑镜像的两步：

```text
$ cd /tmp/r837/base/web/admin
$ pnpm exec tsc -p tsconfig.json ; echo EXIT=$?
src/profiles/profile-test-fixtures.ts(4,22): error TS2307: Cannot find module '../../../../tests/fixtures/contracts/consumer-profiles.json' ...
src/task-center/dev/demo-transport.ts(1,22): error TS2307: Cannot find module '../../../../../tests/fixtures/contracts/file-workflow.json' ...
src/task-center/job-client.test.ts(3,22): error TS2307: Cannot find module '../../../../tests/fixtures/contracts/file-workflow.json' ...
... 共 25 条 error TS（TS2307 加连锁 TS7006）
EXIT=2

$ pnpm exec vite build ; echo EXIT=$?
dist/assets/index-CzHTtsvb.js   754.89 kB │ gzip: 177.65 kB
dist/assets/index-D95tSFRO.css   22.63 kB
✓ built in 240ms
EXIT=0
```

对照实验（同一上下文，把 `tests/fixtures/contracts` 放回去）：

```text
$ pnpm exec tsc -p tsconfig.json ; echo EXIT=$?
EXIT=0
$ pnpm exec vite build | grep index-
dist/assets/index-CzHTtsvb.js   754.89 kB      # 与缺夹具时同一 hash
```

结论：**失败点确实在 `tsc -p tsconfig.json` 这一步，`vite build` 不需要夹具，
有无夹具产物同 hash**。作者的判断与原文一致；`pnpm --filter ... build` 是
`tsc ... && vite build`，所以镜像红在第一步。

被审 head 的隔离上下文（427 文件）里三种跑法：

```text
$ cd /tmp/r837/head/web/admin
$ pnpm exec tsc -p tsconfig.json ; echo EXIT=$?        # 26 条 error TS
EXIT=2                                                 # 预期：typecheck 仍覆盖测试/dev
$ pnpm exec tsc -p tsconfig.build.json ; echo EXIT=$?
EXIT=0
$ pnpm exec vite build | grep index-
dist/assets/index-Rhght7rA.js   754.95 kB

$ cd ../workbench
$ pnpm exec tsc -p tsconfig.build.json ; echo EXIT=$?
EXIT=0
$ pnpm exec vite build | grep index-
dist/assets/index-DgwUJY0_.js   700.74 kB
```

被审 head 上 `tsc -p tsconfig.json` 仍红是**设计使然**（`typecheck` 继续覆盖测试与
dev 源，CI 的 `pnpm typecheck` 跑在整仓上），不是遗漏。PR 正文写
`tsconfig.json` 的 `include: ["src"]`，实际是 `["src", "vite.config.ts"]`，不影响结论。

### 2. 排除是否过宽 —— 否，产品程序里一个文件都没少

用 `tsc --listFiles` 对比两个程序（被审 head，把夹具放回后才可解析全程序）：

| 应用 | 全量程序（`tsconfig.json`） | 生产程序（`tsconfig.build.json`） | 只在全量 |
| --- | --- | --- | --- |
| admin | 378 | 332 | 46 |
| workbench | 478 | 379 | 99 |

「只在生产程序」的集合两边都是**空集**，即生产程序是全量程序的严格子集，没有被排除
模式**新增**进来的文件。被移出生产程序的 46/99 条，逐条只落在四类：

- `node_modules` 的测试专用类型包（`@testing-library/*`、`pretty-format`、
  `@types/aria-query`、`@types/react-dom/test-utils`），admin 21 条、workbench 21 条；
- `tests/fixtures/contracts/*.json`（admin 2、workbench 3）与
  `packages/contracts/src/speechnexus_contracts/file.schema.json`（workbench 1，仅测试导入）；
- `*.test.ts(x)`、`src/test/setup.ts`、`src/**/dev/**`（admin 23、workbench 74 中的其余部分）；
- 被排除的 5 个夹具模块（admin `profiles/profile-test-fixtures.ts`；workbench
  `meetings/meeting-fixtures.ts`、`realtime/realtime-fixtures.ts`、
  `realtime/tts-fixtures.ts`、`voices/voice-fixtures.ts`）。

「产品模块恰好叫 `dev/`」不存在：`web/**/src` 下只有两个 `dev` 目录
（`admin/src/task-center/dev` 2 文件、`workbench/src/file-workflow/dev` 6 文件），
全部是 dev 演练页/演练传输与演示 CSS；`src/test/` 只有两个应用的 `setup.ts`。

5 个夹具模块的导入者全部是 `*.test.ts(x)`（外加 `tsconfig.build.json` 的排除项本身），
示例：`profile-test-fixtures` ← 4 个 `*.test.ts(x)`；`meeting-fixtures` ← 8 个
`*.test.ts(x)`；`realtime-fixtures` ← 9 个 `*.test.ts(x)` 与 `tts-fixtures.ts`；
`tts-fixtures` ← 4 个 `*.test.ts(x)`；`voice-fixtures` ← 2 个 `*.test.ts(x)`。
没有一个非测试源导入它们。

排除模式本身也只覆盖目前的命名法：`web/**` 下 84 个测试文件**全部**是
`.test.ts` / `.test.tsx`（无 `.spec.*`、无其他后缀），所以 `src/**/*.test.ts(x)`
不漏。若将来出现新命名或新的非 `*.test.*` 夹具模块，它会作为生产程序的根文件被
`tsc` 跟上、镜像构建转红，`pnpm verify:web-image` 的 `--build` 半场会先拦住——
失败方向是 fail-closed，不是静默放行。

### 3. 镜像是否真干净 —— 是；顺带澄清两处「看起来像夹具」的字符串

两个镜像都用被审 head 的真实 `docker build` 产出，再在镜像内查：

```text
$ docker run --rm sn837-web-admin:head find /usr/share/nginx/html -type f | sort
/usr/share/nginx/html/50x.html
/usr/share/nginx/html/assets/index-D95tSFRO.css
/usr/share/nginx/html/assets/index-Rhght7rA.js
/usr/share/nginx/html/favicon.svg
/usr/share/nginx/html/index.html          # 共 5 个文件

$ docker run --rm sn837-web-admin:head sh -c 'find / -xdev \( -name tests -o -name "*fixtures*" \
    -o -name "*.test.*" -o -name file-workflow.json -o -name consumer-profiles.json \) 2>/dev/null'
（无输出）

$ docker run --rm sn837-web-admin:head sh -c 'grep -rl "asset-demo\|job-demo\|consumer-profiles\|tests/fixtures" /usr/share/nginx/html'
（无输出）
```

workbench 镜像同样干净（25 个文件，全部 `assets/` 分块 + `index.html`/`favicon.svg`/`50x.html`；
同样的 `find`/`grep` 均无输出）。两个镜像的入口 JS 与我从 COPY 集合单独构建的产物
**sha256 相同**（admin `9a47da5a…`、workbench `f63f8723…`），即镜像里的字节就是 COPY
集合能构建出的那份，不是借整仓兜底编出来的。

「是不是换个名字混进去」也查了：夹具的专有载荷字符串
（`/run/secrets/node-ca.pem`、`session-demo`、`<synthetic-pcm16>`、
`Synthetic transcript.`）在两个镜像里出现 0 次。产物里确实能搜到 `job_demo` 与
`2026-10-23T00:00:0x` 这类字符串，但来源是 **`packages/contracts/src/speech.generated.ts`
（由 `contracts/speech-openapi.yaml` 的 examples 生成的产品契约代码，属 COPY 集合内的
合法输入）**，不是 `tests/fixtures/contracts`；`origin/main` 的产物同样含这两串。
作者用的是 `job-demo`（连字符）、`asset-demo`、`consumer-profiles` 三个标记，恰好
`job_demo`（下划线）不在其列——但这是产品契约字符串，不是漏检。

产物里也没有 dev 演练页：`dist/assets/` 无 `*Demo*` 分块，`任务状态演练`/
`文件转写演练`/`文本配音演练` 文案与 `TaskCenterDemo`/`FileTaskDemo`/`TtsTaskDemo`
标识全部不存在。

### 4. 新检查够不够 —— 动态半场够；静态半场只算「策略前置」，不足的部分已在文档层面标出缺口

- **动态半场（`--build`）确实在 CI 里跑**，不是只有静态半场：CI run `43687682546`
  （head `9cc3c461`）的 `Web: lint, types, tests, build and accessibility` 作业日志里
  有 `$ python3 scripts/dev-env/verify-web-image-context.py --build`、
  `materialised 427 files into /tmp/web-image-context-…`、
  `built @speechnexus/admin, @speechnexus/workbench from the copy set alone`；
  本地同一棵树跑 `pnpm verify:web-image` 也是 5.5 秒、退出 0、427 文件。
- **静态半场只在 `changes` 作业通过 `python3 -m unittest discover -s tests/ci` 跑**
  （CI 日志里 20 个 `test_web_image_context.*` 全部 ok），它**不解析 bare specifier**：
  删掉 `COPY packages/contracts packages/contracts` 后，静态半场仍然 **EXIT=0**，
  只有 `--build` 半场 **EXIT=1**（`error TS2307: Cannot find module
  '@speechnexus/contracts/admin'`）。作者的这条自报属实。
- 这条盲区**没有**写进 `docs/validation/ci-validation.md` 的边界段：新增段落只写了
  「CI 不跑 docker build」「`.dockerignore` 是子集」「不证明最终层内容」，然后说
  「它证明的是构建输入集合：集合外的依赖会让镜像构建失败」——这句话只对
  `--build` 半场成立，对静态半场不成立。实际可达范围里这不会造成假绿：`docs`
  车道只跑静态半场，而 docs 车道不可能改 COPY 集合；改 `deploy/` 或 `web/` 会落到
  `full`/`web` 车道，`--build` 半场必然执行。但边界文字与实现能力不一致，见建议修 S1。

## 逐条判定

### A. 技术核心

- **A1 失败点定位：成立。** 见「核心四问 1」。要点：基线隔离上下文 `tsc` EXIT=2（25 条
  error TS）、`vite build` EXIT=0；加载夹具后 `tsc` EXIT=0 且 `vite build` 同一 hash
  `index-CzHTtsvb.js`。head 上 `tsconfig.build.json` 两个应用都 EXIT=0。
- **A2 排除范围：不过宽。** 全量程序 ⊃ 生产程序且差集全部是测试/夹具/dev 文件；
  两个 `dev` 目录全是演练页；84 个测试文件命名统一；产品源码无一处导入被排除的夹具模块
  （`tsc` 的 exclude 只裁剪根文件、不裁剪被导入文件，所以若产品源码导入它们，它们会
  重新出现在生产程序里——实测没有）。
- **A3 `import.meta.glob` 门控：生效，留有 1 处无害残余。** 生产产物无 dev 分块、无演练页
  代码、`import.meta.env` 出现 0 次。残余：两个 `AppEntry.tsx` 把
  `new URLSearchParams(location.search).get("dev-scenario")` 提到了 `DEV` 三元之外，
  产物里因此留下一个字面量 `dev-scenario` 与一次无副作用的启动读取（admin 产物里就
  是裸表达式 `new URLSearchParams(location.search).get(\`dev-scenario\`);`；
  基线产物 0 次）。`?dev-scenario=` 在产物里**不可达**（`devPages` 为 `{}`,
  `devScenarioPage` 恒 `undefined`，`z=R` 即 `Page=App`），不影响正确性，见建议修 S5。

### B. 新增检查

- **B4 `.dockerignore` 子集：实测与真实 `docker build` 一致。** 两批对照（真实
  BuildKit 上下文 vs `dockerignore_excluded()` 判定），15 条路径全部一致，
  含 `**/node_modules` 命中根目录、`**/dist` 命中根 `dist`、`!keep` / `!keep_root.log`
  这类无前缀取反、以及**被排除父目录下的 `!web/keep.ts`、`!web/admin/src` 仍可救回**
  （Docker 官方文档说不可，但现役 BuildKit 实测可以，脚本的「最后一条匹配胜出」
  也跟着一致）：

  | 路径 | 真实 docker 上下文 | 脚本判定 |
  | --- | --- | --- |
  | `node_modules/root.txt`（`**/node_modules`） | 排除 | 排除 |
  | `web/node_modules/pkg/x.js`（`**/node_modules`） | 排除 | 排除 |
  | `web/keep.ts`（`web` + `!web/keep.ts`） | **保留** | 保留 |
  | `web/drop.ts`（`web`） | 排除 | 排除 |
  | `dist/root.txt`（`**/dist`） | 排除 | 排除 |
  | `web/admin/dist/bundle.js`（`**/dist`） | 排除 | 排除 |
  | `secrets/token`（`secrets`） | 排除 | 排除 |
  | `secrets/keep.txt`（`secrets` + `!secrets/keep.txt`） | **保留** | 保留 |
  | `web/admin/src/main.ts`（`web/*` + `!web/admin/src`） | **保留** | 保留 |
  | `web/root.log` / `web/logs/a.log`（`web/*`） | 排除 | 排除 |
  | `keep/x.txt`（`!keep`）、`keep_root.log`（`*.log` + `!keep_root.log`） | 保留 | 保留 |

  已知边界（`\` 转义、模式内 `!`、嵌套 `.dockerignore` 不读）本仓库没有用到，也未发现
  「脚本说在、Docker 说没有」的假绿方向。按「子集」写进文档是诚实的。
- **B5 静态半场盲区：如实存在，CI 动态半场在跑，文档边界段未写明。** 见「核心四问 4」。
- **B6 三条反例：全部独立复现。**
  1. **产品源码引入 COPY 集合外文件**：在 PR head 全量副本的
     `web/admin/src/App.tsx` 首行加 `import bindings from "../../../contracts/bindings.json"`，
     整仓 `pnpm --filter @speechnexus/admin build` **EXIT=0**（这正是 CI 看不见的原因），
     新检查 **EXIT=1** 并点名
     `src/App.tsx(1,22): error TS2307: Cannot find module '../../../contracts/bindings.json'`。
     注意：这条只有 `--build` 半场会红，**静态半场是绿的**（它只拦 `tests/**` 导入），
     与 B5 是同一处盲区。
  2. **删掉 `COPY packages/contracts packages/contracts`**：真实
     `docker build` **EXIT=1**，`src/profiles/profile-client.ts(1,39): error TS2307:
     Cannot find module '@speechnexus/contracts/admin'`（与作者自报逐字一致，
     `pnpm install` 仍成功，红在 tsc）；新检查 `--build` **EXIT=1**。
  3. **新检查必红**：同上，输出
     `FAIL: pnpm --filter @speechnexus/admin build failed inside the
     deploy/images/web.Dockerfile copy set (exit 1); the copy set is missing a build input`。
  4. **`.npmrc` 缺失**：把 `.npmrc` 从 COPY 行去掉后，物化上下文里
     `pnpm install --frozen-lockfile` **EXIT=0**、两个应用的
     `tsc -p tsconfig.build.json` + `vite build` 都 **EXIT=0**——即**真实 `docker build`
     会绿**；静态检查 **EXIT=1**：
     `FAIL: deploy/images/web.Dockerfile: the install and the typecheck read these root
     files, but the COPY set omits them: .npmrc`。这是**真实的策略分叉**（`.npmrc` 是
     `engine-strict=true`、`save-exact=true`，缺了它镜像内的引擎/依赖策略与 CI 不同），
     不是过度严格；问题只在文案——`tsc` 不读 `.npmrc`，install 缺它也不失败（建议修 S3）。

### C. 镜像不含测试数据

- **C7 成立。** 见「核心四问 3」：两镜像的 `find`/`grep` 均无命中，产物与 COPY 集合
  构建的 hash 一致，夹具载荷字符串 0 次；`job_demo`/时间戳来自生成的产品契约，
  不是测试数据。另外单独复现了 `#834` 本身：`origin/main` 的 admin 镜像
  `docker build` **EXIT=1**（`profile-test-fixtures.ts … TS2307`），
  被审 head 两个 `APP_NAME` 都 **EXIT=0**。

### D. 范围与偏差

- **D8 `docs/validation/ci-validation.md` 的改动只含事实性更新。** 三个 hunk：
  ① 去掉 `deploy/images/web.Dockerfile:12` 里的行号——实查基线第 12 行正是
  `COPY web web`，被审 head 移到了第 21 行，去掉行号是正确的时效性修正，且只改引用
  形态、不改口径；② `web` 作业职责补「Web 镜像构建上下文完整性」；③ 新增「Web 镜像
  构建上下文门禁」章节（6 行判据表 + 边界段 + 三条本地命令）。**没有**顺手改范围
  白名单、覆盖率、门禁映射或任何既有判据。缺两处（静态半场盲区、验证指南 Web 行）见
  S1/S2。
- **D9 变更范围：均在「修复这条」的必需链上，没有改业务逻辑或测试语义。**
  13 条路径全部可归因：Dockerfile、两个 `tsconfig.build.json`、两个 `AppEntry.tsx`
  （dev 演练页改 glob，属修 #834 的手段）、两个 `package.json` 的 `build` 脚本、
  守卫脚本与其测试、`deploy/README.md`、`docs/validation/ci-validation.md`；
  另有两条**超出任务给的目录白名单但属接线**：`.github/workflows/ci.yml`（+9 行，
  在 `web` 作业 `pnpm build` 之后插入这一步）与根 `package.json`（+1 行
  `verify:web-image` 脚本）。作者只自报了 `docs/validation/ci-validation.md`
  一处「白名单外」，这两条未自报，但都是必需的接线、不含业务逻辑。
  既有测试**零修改**（`tests/**` 只有新增文件），`pnpm test` 改前改后同为
  admin `151 / 19 files`、workbench `1064 / 65 files`。
- **D10 两处数字订正：都正确。** `git grep -l 'tests/fixtures/contracts' origin/main -- 'web/**'`
  实测 **35** 个文件（`web/admin/` 11、`web/workbench/` 24），不是 33；
  `pnpm test` 实测 workbench **1064**（65 files），`origin/main` 与 head **同值**，
  所以 1063 是任务给的基线写错，订正成立。
  （PR 正文的分类表有一处加总不闭合：28 test + 4 夹具模块 + 1 转发 + 4 dev = 37，
  实测 35 = 28 test + 4 夹具模块 + **3** 个 dev 传输文件；`tts-fixtures.ts` 在排除
  清单里，但不是那 35 个引用文件之一。仅正文表述，见 S4。）

### E. 例数与 CI

全部在被审 head 的 detached worktree 上实跑：

| 检查 | 结果 |
| --- | --- |
| `python -m pytest tests/ci -q` | **390 passed**（1357 subtests）；`origin/main` 同环境 **370 passed**（816 subtests），差值 +20 恰为新测试文件的 20 个 test 方法 |
| `python -m unittest discover -s tests/ci` | `Ran 390 tests … OK` |
| `pnpm test` | admin **151 / 19 files**、workbench **1064 / 65 files** |
| `pnpm typecheck` / `pnpm lint` / `pnpm build` / `pnpm format:check` | 全部通过（typecheck 仍用 `tsconfig.json`；CI 的 `pnpm typecheck` 步骤仍在 `pnpm build` 之前，测试/dev 源的类型检查没有丢） |
| `pnpm verify:web-image` | 通过：427 文件、`built @speechnexus/admin, @speechnexus/workbench from the copy set alone` |
| `ruff format --check` / `ruff check`（两个新 py 文件） | 通过 |
| `gh pr checks 836` | run `43687682546`（head `9cc3c461`）：20 pass / 1 skipping（`docs_format`，docs 车道不适用）/ 0 fail，三个受保护检查均 pass |

`--build` 半场给 `web` 作业加的钱很少：CI 日志里该步骤 `06:58:42` 起、`06:58:48`
已输出构建完成，本地热 store 下 5.5 秒。

## 必须修清单

**无。** 未发现会阻塞合并的正确性、覆盖或门禁问题；上述每一个作者自报的可核事实都被
独立复现到，没有发现夸大或错报。

## 建议修（不阻断）

- **S1（文档边界，最值得补）** `docs/validation/ci-validation.md` 新增章节的边界段补一句：
  静态半场不解析 bare specifier（`@speechnexus/contracts` 之类），`COPY packages/contracts`
  这类**非测试输入**的缺失只有 `--build` 半场能看见；把「集合外的依赖会让镜像构建失败」
  限定到 `--build` 半场。这与作者已自报的盲区一致，只是没落到权威文档。
- **S2（验证入口一致性）** `docs/agent-verification.md` 的 Web 行（第 23 行）仍只列
  `format:check`/`lint`/`typecheck`/`test`/`build`/`test:a11y`，没有新命令；
  而 `web/AGENTS.md` 明确规定「验证使用验证指南的 Web 行」。建议补
  `pnpm verify:web-image`，否则本地按指南验证的人不会跑到这条新门禁。
- **S3（文案精度）** `ROOT_CONFIG_INPUTS` 的失败文案把 5 个根配置混成一句
  「the install and the typecheck read these root files」：`.npmrc` 不被 `tsc` 读，
  install 缺它也不失败（实测两者都 EXIT=0），它是**策略**输入而非构建必需输入。
  建议把「构建必需」与「策略一致（engine-strict/save-exact）」分开报。
- **S4（PR 正文）** 修正分类表加总：35 = 28 test + 4 夹具模块 + 3 dev 传输文件；
  `tts-fixtures.ts` 是被排除文件但不是引用文件之一；「dev 2+2」应为 1+2。
- **S5（可选，产物洁净度）** 两个 `AppEntry.tsx` 可把 `scenario` 的读取放回
  `import.meta.env.DEV` 三元内，避免生产产物残留 `dev-scenario` 字面量与一次
  无副作用的 `new URLSearchParams(location.search)`。不影响可达性。
- **S6（可选，防御未来配置）** `tsconfig.build.json` 用 `exclude` 覆盖父配置的
  `exclude`（TS 的 `extends` 语义），当前父 `tsconfig.json` 没有 `exclude` 所以无害；
  日后父配置若加 `exclude` 会被静默覆盖。可加一行注释，或让守卫断言父 `exclude` 为空。

## 只能依赖作者陈述的点

以下我**没有**独立验证，或只验证到间接证据，报告中按作者陈述处理：

1. **`origin/main`（`bfafc894`）时的镜像构建原始日志**：我在当前 `origin/main`
   （`6c886c86`）上重跑同一条 `docker build` 得到同样的 `TS2307` 红，因此
   「#834 在 main 上就构建失败」这一点是独立成立的；但作者贴的 `#19 ERROR … exit code: 1`
   那一份具体日志我没有逐字比对（我的基线构建输出格式/编号不同）。
2. **作者本地/CI 的 pnpm store 预热程度**：`--prefer-offline` 的安装耗时会随 store
   状态变化，我只核到「能装、能建」，未核 CI 的绝对耗时。
3. **`.dockerignore` 全语义等价**：我只用两组共 15 条路径做了真实 BuildKit 对照，
   没有覆盖 `\` 转义、模式内 `!`、非根 `.dockerignore` 等 Docker 语义边角。
4. **dev 演练页在真实后端下的行为**：我用 `pnpm dev` + Chromium 验证了 5 个页面能渲染
   （`h1` 正确、0 个 console/page error，`ALL_DEV_PAGES_OK`），但没有接真实语音后端
   跑演练流程；作者报的「5/5 PASS、无 page error」与我的结果一致。
5. **镜像运行期（nginx 模板、上游代理、healthcheck）**：与作者自报一致，未验证；
   本次只验证构建与镜像内容。

## 过程时间线（CST，2026-11-15）

- 15:10 `git fetch`，建 `/private/tmp/sn837`（base `origin/main` `6c886c86`），
  确认 `docs/reviews/` 59 份既有报告且目标文件名不存在；读 `web/AGENTS.md`、
  `docs/validation/ci-validation.md`、`deploy/README.md`。
- 15:11 用 `git archive` 物化两个隔离上下文（基线 426 文件、head 427 文件，均无 `tests/`）。
- 15:12 基线 `tsc` EXIT=2 / `vite build` EXIT=0（`index-CzHTtsvb.js`）；
  放回夹具后 `tsc` EXIT=0、`vite build` 同 hash。head `tsconfig.build.json` 两个应用
  EXIT=0，产物 `index-Rhght7rA.js` / `index-DgwUJY0_.js`；产物 grep 无 dev 分块/夹具标记。
- 15:13 `tsc --listFiles` 程序差集（admin 378→332、workbench 478→379，生产侧无新增）；
  dev/test 目录与 `import.meta.glob` 全仓审计。
- 15:14 启动 admin 镜像构建（后台），同时核对 CI run `43687682546` 的 Web 作业日志。
- 15:15 建 `/tmp/r837/mut`（head 全量副本）：静态半场绿灯；删 `COPY packages/contracts`
  后静态仍绿、`--build` 红。
- 15:16 `.dockerignore` 两批真实 `docker build` 对照实验（15 条路径全一致）。
- 15:17 B6-1 越界导入：整仓 build EXIT=0、静态绿、`--build` 红。
- 15:18 两个镜像构建完成（均 EXIT=0）；C7 镜像内容、产物 hash 一致性、载荷字符串。
- 15:19 基线镜像 `docker build` EXIT=1（复现 #834）；删 contracts 的 head 镜像 EXIT=1。
- 15:20 `.npmrc` 缺失对照（install+build 绿、静态红）。
- 15:21 `tests/ci` 390/370、`pnpm test` 151+1064、`typecheck`/`lint`/`build`/`format:check`、
  `verify:web-image`。
- 15:22 真实浏览器 dev 页面 5/5（`localhost:5173`、`localhost:5174`，0 error），随后停服。
- 15:23 写报告；清理：`docker rmi` 本轮的 `sn837-*` 与残留的 `sn836-*`，
  删除 detached worktree `/private/tmp/sn837-head`；`/tmp/r837` 下过程日志保留备查。

## 复现命令（节选）

```sh
# 1) 隔离上下文：只含 builder 阶段 COPY 集合
git archive origin/main -- package.json pnpm-lock.yaml pnpm-workspace.yaml tsconfig.base.json \
  web packages/contracts scripts/dev-env/validate-design-tokens.mjs \
  scripts/dev-env/validate-design-tokens.d.mts | tar -x -C /tmp/r837/base
cd /tmp/r837/base && pnpm install --frozen-lockfile --prefer-offline
(cd web/admin && pnpm exec tsc -p tsconfig.json ; echo EXIT=$?)   # 2
(cd web/admin && pnpm exec vite build ; echo EXIT=$?)             # 0, index-CzHTtsvb.js

# 2) 真实镜像（用完清理）
docker build -f deploy/images/web.Dockerfile --build-arg APP_NAME=admin -t sn837-web-admin:head .
docker run --rm sn837-web-admin:head sh -c 'find / -xdev -name "*fixtures*" -o -name "*.test.*"'
docker rmi sn837-web-admin:head sn837-web-workbench:head

# 3) 新门禁（在 head 树上）
pnpm verify:web-image
python3 -m unittest tests.ci.test_web_image_context -v
```
