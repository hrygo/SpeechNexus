---
title: PR #930 非作者审查：镜像契约摘要复核与跨机搬运
status: active
---

# PR #930（`build/same-version-workbench`）镜像契约摘要复核的非作者审查

被审对象：PR #930《build: 本机构建同版工作台镜像并给出跨机导入方式》，
head `f3a78b15b02d1485854151a11ab7da47e9cad5f6`（两个提交：`e0e0f3d8` 主体 + `f3a78b15` mypy 类型收窄），
分叉点 = `origin/main` = `acf633ff9e71f573dbfef5082d0680e2a3aefa41`。按分叉点口径
`git diff acf633ff f3a78b15` = **4 文件 +334/−0**：`deploy/README.md`(+35)、
`docs/operations/same-version-check.md`(+2)、新增 `scripts/dev-env/check-image-contract-digest.py`(212 行)、
新增 `tests/ci/test_image_contract_digest.py`(85 行)。本报告未使用两点 `git diff origin/main f3a78b15`
——本次两者数值恰好相同，但口径仍按分叉点。

审查方式：`/private/tmp/sn932` 是从 `origin/main` 新建的审查分支 `review/930-image-digest`，
只放本报告；被审 head 用 `git show f3a78b15:<path>` 读取，构建/测试在签出于被审 head 的树上做。
临时镜像（`review930/*`）与临时 worktree 复核后清理；**作者的
`speechnexus/web-workbench:acf633ff…` 与 `speechnexus/web-workbench:5cd5a5d0` 两个镜像未删除、未改写**。

环境：macOS arm64 27.0.1，Docker 29.9.3（overlayfs），Python 3.14.9（`uv sync --locked
--all-packages --all-groups` 成功）、pnpm 12.14.0（`pnpm install --frozen-lockfile` 成功）。核验时间 2026-11-17。

## 结论

**不能合（当前）**：两处低成本**必须修**，都不涉及返工设计——(M1) `--expect` 的来源口径
在新增文档里被写成「取部署自报值」，同一段却声称这样能挡住「`docker load` 送来的另一个提交」，
两者不能同时成立（实测见我构造的反例）；(M2) 本 PR 的产品就是退出码语义，但 `main()` 的候选裁决
（0 个 / 多个候选 → 1）**没有任何用例**，删掉「多候选即失败」后 4 条用例仍全绿。
技术主体成立：`恰一个候选` 的判据在真实 14 chunk 产物与「同名摘要出现在第二个 chunk」两种形态下
都正确；跨机 tar 确实只含最终镜像的 12 层；作者自述的四条反例全部逐项复现。其余为
「可接受但需记录」（§11），其中镜像 ID 不可由重建复现（§7）值得写进文档口径。

## 0. 复核记录

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 分叉点口径 | `git diff --stat acf633ff f3a78b15` | 4 文件 +334/−0 |
| 镜像身份 | `docker image inspect … --format '{{.Id}} {{len .RootFS.Layers}} {{.Size}}'` | `sha256:25dd28647797…`、12 层、`95919908` B |
| 标签 | `--format '{{json .Config.Labels}}'` | `org.speechnexus.source.commit=acf633ff9e71…`、`dirty=0`、`app=workbench`（另有 nginx 基础镜像自带 9 条） |
| 摘要复核（默认路径） | `uv run python scripts/dev-env/check-image-contract-digest.py speechnexus/web-workbench:acf633ff…` | `EXIT=0`，烘焙值 = 离线重算值 = `sha256:ad581b46…7863`，0.26 s |
| 新增单测 | `uv run pytest tests/ci/test_image_contract_digest.py -q` | 4 passed |
| 格式/静态/类型 | `ruff format --check`、`ruff check`、`mypy`（三个新文件/新增两文件） | 全通过（`2 files already formatted`、`All checks passed!`、`Success: no issues found in 2 source files`） |
| 构建产物复现 | `docker build --target build -f deploy/images/web.Dockerfile --build-arg APP_NAME=workbench -t review930/web-dist:check .` | `exit 0`；`/workspace/web-dist` 树摘要 `a13ee6f2…34a5`、24 文件 |
| 镜像重建（最终阶段） | 同 Dockerfile + 同三个 label，另打 `review930/web-workbench:rebuild{,2}` | `exit 0`；12 层 diffID 与作者镜像**全等**，image ID **不等**（§7） |
| 树摘要对照 | 见 §5 | 本机 `pnpm --filter @speechnexus/workbench build` 的 `dist` 树摘要 = `a13ee6f2…34a5`/24 文件 = 镜像 `html` 减 `50x.html` |
| 反例重做 | 见 §9 | 4/4 复现；另加 2 组变异（§9.5） |
| 篡改还原 | `git status --porcelain` | 空（变异在 worktree 内临时施加后用备份还原，未提交） |

## 1. 作者自述逐条核对

| 自述 | 判定 | 依据 |
| --- | --- | --- |
| 镜像 `speechnexus/web-workbench:acf633ff…`，label `source.commit`/`dirty=0`/`app=workbench` | **成立** | 见 §0 镜像身份行；`acf633ff9e71f573dbfef5082d0680e2a3aefa41`、`0`、`workbench` |
| 烘焙摘要与离线重算逐字相等（`sha256:ad581b46…7863`） | **成立** | 默认路径 `EXIT=0`；bundle 里 `index-npRtzbgA.js` 唯一真摘要 = 重算值 |
| 脚本 `docker create`+`docker cp` 取 bundle 本地扫描，不依赖镜像内 grep/sh | **成立** | `check-image-contract-digest.py:93-111` 只用 `docker create/cp/rm`，扫描在宿主 Python 用 `pathlib` 读字节；`docker run` 一次都没有，镜像内代码不执行 |
| 排除 `sha256:(.)\1{63}`；bundle 里 `aaaa…` 出现 2 次 | **成立** | `index-npRtzbgA.js` 内 `aaaa…` 恰好 2 次，真摘要 1 次；admin 产物另有 5 种重复字符夹具（§4） |
| 剩余恰 1 个候选才算取到；退出码 `0/1/2` | **成立（口径需精确到「不同取值」）** | 14 个 chunk 的镜像只有 1 个候选值；第二个 chunk 带**同名**摘要时 `EXIT=0` 并列出两个文件（§2） |
| 反例：改摘要末位 → `EXIT=1` 且仍打印同一 source commit | **成立** | §9.1：`baked …7864` vs `expected …7863`，label 仍 `acf633ff…` |
| 反例：旧镜像 `5cd5a5d0` → `EXIT=1` + `same-version guard ABSENT` | **成立** | §9.2；该镜像同时带 `aaaa…` 夹具，说明「0 候选」不是被夹具排除规则误造出来的 |
| 反例：`--expect` 错值 → `EXIT=1`；镜像不存在 → `EXIT=2` | **成立** | §9.3、§9.4（`--bundle-root` 指到不存在目录同为 `EXIT=2`） |
| `--target build` 的 `web-dist` 树摘要 `a13ee6f2…34a5`（24 文件）= 镜像发布树减 `50x.html` | **成立（字面重跑 + 本机 dist 双重确认）** | §5：三者同摘要同文件数 |
| 跨机：95.9 MB / 12 层；save tar 26,725,888 B；`gzip -9` 26,558,151 B，只省 0.6% | **数值成立，一处措辞需修正** | tar 大小逐字节相同；`gzip -9` 本机 26,558,167 B（16 B 差来自 gzip 头文件名/时间戳），省 167,721 B=0.63%；「95.9 MB unpacked」是 Docker 记账值，实测未压缩层 tar 流 66,159,104 B（§11 R2） |
| 新增 4 例、无需 Docker | **成立** | 4 passed/0.01 s；`scan()` 只吃临时目录 |

## 2. 风险 1：「恰一个候选」在多 chunk 构建下成立吗

**结论：成立，且判据实际是「恰一个不同取值」而不是「恰一个文件」——这正好是代码分裂最可能产生的形态。**

- 真实 workbench 镜像**已经是 14 个 JS chunk**（`AssistantHost`、`BooksHost`、`ManuscriptHost`、
  `RealtimeHost`、`worklet`、`formats`…），不是作者描述里「只看到 `index-npRtzbgA.js` 一个 chunk」的字面意思；
  真摘要只在入口 chunk 里出现 1 次，另 13 个 chunk 一次都没有，扫描结果仍是 1 个候选。
- 我构造了两种临时镜像（在作者镜像基础上加第 15/16 个 chunk 后 `docker commit`，不接触被保护镜像）：
  - **第二个 chunk 带同一摘要**（Vite 把共享常量复制进新 chunk 的形态）：`EXIT=0`，输出
    `baked digest sha256:ad58… (BooksHost-dup-same.js, index-npRtzbgA.js)`。`scan()` 以摘要值为键
    (`check-image-contract-digest.py:128-130`)，同名多文件合并成 1 个候选并列出全部来源，
    因此「未来 code-splitting」不会误判，这正是应该的行为。
  - **第二个 chunk 带另一个真摘要**（两个契约修订共存 / 诱饵）：`EXIT=1`，逐条列出两个候选与所在文件。
    fail closed，没有静默取胜者。
- admin 侧：本机 `pnpm --filter @speechnexus/admin build` 的产物只有 1 个 chunk、1 个候选、5 种重复字符夹具，
  同样成立（但注意 §11 R6：本机没有 `acf633ff` 的 admin **镜像**，这一条是 dist 证据）。
- 残余风险不是「多 chunk」，而是**任何别的** `sha256:<64hex>` 字面量进入产物（依赖完整性表、内嵌哈希等）：
  会出现 1 个诱饵候选 → `EXIT=1` 误报（fail closed，可接受但费人工）。当前实测没有（§4）。
  建议只改失败输出：多候选时额外打印 `expected` 是否在候选中，让人一眼区分「一真一诱饵」与
  「镜像里根本没有期望值」；不必改判据——「取所有候选并要求全部相等」的结论与现行实现等价
  （任一诱饵都会红），换写法不增加判别力。

## 3. 风险 2：`--expect` 的循环性 —— 是，文案把「配对一致」当成了「来源可证」

新增文案（`deploy/README.md:539-546`）与 `docs/operations/same-version-check.md:77` 的口径是：

> 有工作树时与本工作树重算值比对；**无工作树时用 `--expect` 传入部署自报值**

以及同一段紧接着的承诺：

> A `docker load` that delivered a different commit, or an image that predates the
> same-version guard, therefore fails instead of merely carrying the tag.

两者不能同时成立。`--expect` 若取「接受镜像的那套部署自己的 `GET /v1/nexus/version`」，
检查就退化为「前端 bundle 与该 API 同契约」——这是有价值的配对判据（与运行期守卫同一件事），
但它**不绑定提交**：整套部署（新旧一致）是旧提交时，两个值相等，脚本输出 `OK`，
而 label 仍宣称 `acf633ff`。作者自己已经证明 label 看不住被改过的镜像（§9.1），
因此「镜像来自待核对提交」这件事现在只剩一条独立证据：在该提交上离线重算，或取构建时记入
发布记录的摘要。文档没有写这一句，反而明确邀请自知值。

实测反例（§9.6）：把烘焙摘要末位改掉、label 不动，再把这个改后的值当作 `--expect` 传入 →
`EXIT=0`，输出 `source commit acf633ff9e71…` 与 `OK`。这不是工具 bug，是**用法与文案**的问题：
脚本无从判断 `--expect` 的来源，能修的是文档与 `--help`。

建议改法（两句，代码不动）：在 `deploy/README.md` 该段与 `same-version-check.md:77` 都写明
「`--expect` 应取**要核对提交上的离线重算值**，或构建机记录进发布记录的摘要；取自被核对部署自报值
只能证明该前端与该 API 同契约，不能证明镜像来自该提交」。同时 `--help`
（`check-image-contract-digest.py:140-145`）的示例也一样收窄。

## 4. 风险 3：夹具排除规则是否过窄 —— 今天够用，但它是**形状判据**

真实镜像发布树（25 文件、14 个 JS）里 `sha256:` 字面量只有两类，全量如下：

| 字面量 | 次数 | 位置 | 性质 |
| --- | --- | --- | --- |
| `sha256:aaaa…aaaa`（64 个 a） | 2 | `assets/index-npRtzbgA.js` | 契约夹具（`contracts/**` 的 example，生成消费者带进 bundle） |
| `sha256:ad581b46…7863` | 1 | `assets/index-npRtzbgA.js` | 本提交的契约摘要 |

- **没有**依赖完整性哈希、**没有** source map（bundle 里无 `.map`、无 `sourceMappingURL`）、
  **没有** Vite 的 `?v=` 版本串（在 `realtime-*.js` 里 grep 到的 `?v=` 是 JS 三元表达式，不是 URL 参数）。
- `contracts/` 源码里其实有 12 种重复字符值（`111…`–`ddd…`）；admin 产物带进 5 种
  （`777/888/aaa/bbb/ccc`），workbench 只带进 `aaa`。规则 `sha256:(.)\1{63}` 覆盖全部 12 种，
  不是只覆盖 `aaaa…`，这一点成立。
- 形状判据的边界：仓库里存在一个**非重复字符**的 `sha256:76d56a3a…`，位于
  `web/workbench/src/manuscripts/download-content.test.ts`（测试文件，不在生产 TS program 里，
  实测镜像 bundle 不含它）。若将来某个夹具/依赖把这类值带进产物，就会成为第二个候选 → `EXIT=1`。
  当前无此问题；建议在脚本 docstring 写明「排除是按形状，第二种真字面量出现时必须人工判读」，
  否则下一个人只会看到「多个候选」而不知从何查起。

## 5. 风险 4：树摘要排除 `50x.html` —— 排除是对的，理由是**来源**不是名字

- 实际内容：app 层（`docker save` 的第 12 层）里正好 24 个文件，与 `web-dist` 一一对应；
  镜像 `html` 共 25 个，多出来的 `50x.html` 来自 nginx 基础镜像
  （`nginxinc/nginx-unprivileged:1.31-alpine` 的 `/usr/share/nginx/html/50x.html`，
  `sha256:876766b4…` 逐字节相同，位于基础层；基础镜像同一目录里的 `index.html` 被 app 层覆盖，
  所以「并集」正好是 24+1）。因此排除它是**去掉基础镜像自带文件**，不是抹掉漂移。
- 三重独立对照（同一提交）：本机 `pnpm --filter @speechnexus/workbench build` 的 `dist` 树 =
  `a13ee6f2…34a5`/24 文件；`docker build --target build` 的 `/workspace/web-dist` 树 = 同摘要同文件数；
  镜像 `html` 减 `50x.html` = 同摘要同文件数。作者旁证成立。
- 唯一可能的盲区：若将来 `web/<app>/public/50x.html` 被加进 dist（Vite 会拷进 `dist/`），
  app 层就会覆盖基础镜像的同名文件，而「按名字排除」会让这处漂移静默。当前没有该文件，
  危害也限于同一个静态错误页；更稳的写法是「镜像树 == `--target build` 树 ∪ 基础镜像自带的
  `50x.html`」（比来源），或断言多出来的文件其 sha256 等于基础镜像那个。记为建议，不阻塞。

## 6. 风险 5：跨机搬运的实际可达性 —— tar 只含最终镜像，比例可解释

对 `docker save` 产物直接拆包核对（`web.tar` 26,725,888 B）：

| 项 | 实测 |
| --- | --- |
| tar 条目 | 23（19 个 blob + `manifest.json` + `index.json` + `oci-layout`），无其它元数据文件 |
| 层数 | 12（与 `RootFS.Layers` 一致），层 blob 合计 26,683,595 B |
| 层内容 | 基础镜像 9 层（alpine + nginx apk 等）+ 3 个 `COPY` 层：`nginx.conf` 模板、`19-configure-upstreams.envsh`、24 个页面文件；**没有** `/workspace`、`node_modules`、pnpm store、源码、`.git`、`tests/**` |
| 未压缩层 tar 流 | 66,159,104 B（逐层解压求和），压缩比 2.48×；单层 2.14×–9.35×（nginx apk 层 2.43×，页面层 9.35×） |
| `gzip -9` | 26,558,167 B，比 tar 少 167,721 B（0.63%） |

- 「只含最终镜像」成立，原因不是 `docker save` 的聪明，而是**多阶段构建**：`node:26.12.1-bookworm-slim`
  build stage 从未进入最终 `FROM nginxinc/…` 的层链，`docker save` 只是把最终镜像的层原样写出。
  我逐层列出了全部条目（合计 2,246 条，最大一层 966 条），没有一条命中 `workspace|node_modules|pnpm|web-dist|/src/|.git`。
- 比例合理：95.9 MB 是 Docker 对该镜像的记账值（`docker images`/`inspect .Size`），
  实测压缩后 26.68 MB、未压缩层流 66.16 MB；基础层是 apk 装出的二进制/库（压缩 2.1–2.4×），
  最大的 16.5 MB 压缩层未压缩 40.2 MB，页面 JS 层 3.90 MB 压缩到 0.42 MB（9.35×）。
  同一引擎上 `alpine:3.20` 也出现 `.Size`(13,580,309) ≠ 层流和(9,107,968) 的差额，
  说明这是 Docker Desktop/containerd 的记账方式，不是本镜像的特征（§11 R2）。
- `docker load -i web.tar` 在本机把 tag/ID 恢复为 `sha256:25dd2864…`（与作者一致），
  传输出错则 ID 变化这条判据成立——但见 §7，**不要把 ID 当成构建复现指纹**。

## 7. 额外发现：镜像 ID 不能由重建复现（不影响本 PR 的用法，但值得写清）

用同一 Dockerfile、同一 `APP_NAME=workbench`、同三个 label，在同一台机器上重建两次：

| 构建 | image ID | 12 层 diffID | config 差异 |
| --- | --- | --- | --- |
| 作者镜像 | `sha256:25dd2864…` | 基准 | `history[31].created_by = "EXPOSE &{[{{61 0} {61 0}}] 0x4000314140}"` |
| 我的重建 1 | `sha256:e0ce96c8…` | **与作者全等** | 仅该指针 → `0x40002b8340` |
| 我的重建 2 | `sha256:2d9220cd…` | **与作者全等** | 仅该指针 → `0x4000698040` |

即：页面内容、层、`created` 都相同，ID 只因 BuildKit 写进 `created_by` 的 Go 指针地址而每次不同
（`EXPOSE` 元数据步骤在每次构建重新生成）。结论：

- `deploy/README.md` 用 ID 判断 **`save`/`load` 是否被改坏**是正确用法（同一份字节恢复同一 ID，我已复现）。
- 但「两台机器各自构建、ID 相同才算同版」不成立；同版判据只能是层/树/契约摘要。
  建议在该段加半句「ID 只用于传输完整性，重建会得到不同 ID（BuildKit 把指针地址写进
  `EXPOSE` 的 history），构建同版以树摘要与契约摘要判断」。这属于记录类，不阻塞。

## 8. 风险 6：泄露面 —— 不打印凭据，唯一的外泄是错误路径里的绝对路径

脚本正常输出（实测原文）只有 6 行：镜像引用、label 里的 commit/dirty、bundle 根（**镜像内**路径
`/usr/share/nginx/html`）、脚本数、`guard present/ABSENT`、夹具摘要、烘焙值+来源文件名、期望值。
不含：容器 ID、临时目录路径、registry 地址、`DOCKER_HOST`、环境变量、任何凭据；`--expect`/摘要本身是公开值。
`docker` 的 stderr 只在失败时原样转发（例如 `No such image: <脚本入参>`），仍是操作者自己给的引用。
镜像内代码不执行（只用 `create`/`cp`/`rm`），不存在「跑镜像里的东西」这条面。

唯一的路径外泄：`load_generator()`（`check-image-contract-digest.py:62-70`）只对
`spec is None` 走友好 `abort()`；生成器文件**缺失**时 `exec_module` 抛未捕获的
`FileNotFoundError`，traceback 里带宿主绝对路径（我在 `/tmp` 副本上实测：
`FileNotFoundError: … /private/tmp/r930/leak/scripts/dev-env/generate-contract-bindings.py`），
且进程退出码是 **1**（被文档解释为「不相等/多候选/零候选」），而 docstring 承诺这种
「跑不起来」应是 **2**（`check-image-contract-digest.py:29-32`）。见 §11 R1。

## 9. 风险 7：反例重做（4 组作者 + 2 组自加）

### 9.1 改烘焙摘要末位一个字（作者反例 1）

用 `docker create` + `docker cp` 把入口 chunk 里 `…7863` 改成 `…7864` 后 `docker commit`
（label 由容器配置继承，未改动），得到 `review930/tampered-digest`：

```
source commit    acf633ff9e71f573dbfef5082d0680e2a3aefa41 (dirty=0)
same-version guard present
baked digest     sha256:ad581b4691e104fea9fad79d28fe3b0a5b08faace31156bdb7b2fe7d5c9c7864 (index-npRtzbgA.js)
expected         sha256:ad581b4691e104fea9fad79d28fe3b0a5b08faace31156bdb7b2fe7d5c9c7863
EXIT=1
```

label 看不住、摘要能看住——作者结论成立。

### 9.2 旧镜像 `5cd5a5d0`（作者反例 2）

`EXIT=1`；输出 `same-version guard ABSENT`；`expected` 仍打印本树重算值；
stderr 是 `bundle bakes in no contract digest … predates the same-version guard (#853)`。
该镜像的 bundle 里仍有 `aaaa…` 夹具（14 个 JS），因此「0 候选」是镜像真的没有真摘要，
不是排除规则把真值筛掉了。

### 9.3 `--expect` 错值（作者反例 3）

对作者镜像传 `--expect …7864`：`baked …7863` vs `expected …7864`，`EXIT=1`，stderr 指两侧可能不一致。

### 9.4 镜像不存在（作者反例 4）

`EXIT=2`，`docker image inspect … failed: … No such image: …`；另外 `--bundle-root /nonexistent/html`
也是 `EXIT=2`（`publishes no bundle at …; pass --bundle-root`）。

### 9.5 自加 1：把候选判定改成「取第一个候选」——没有用例变红（覆盖缺口）

在 worktree 内把 `main()` 里 `if len(candidates) > 1:`（`check-image-contract-digest.py:185-194`）
整段删除（即多候选时静默取第一个），`uv run pytest tests/ci/test_image_contract_digest.py -q`
仍是 **4 passed**。原因：4 条用例只测 `scan()` 的输出，`main()` 的裁决与退出码从未被调用。
对照变异：把 `scan()` 改成「只保留第一个候选」，第 3 条
`test_two_distinct_digests_stay_ambiguous_for_the_caller_to_refuse` 变红——说明测试钉住的是
「scan 返回两个候选」，**没有**钉住「工具对两个候选返回 1」。这是覆盖缺口，不是当前行为错误：
现状实测 `EXIT=1`（§2 的 diff 镜像）。修法很便宜：把 `候选 → 退出码` 抽成纯函数，
加一条覆盖 `0 候选→1`、`多候选→1`、`不相等→1`、`相等→0` 的表驱动用例（仍不需要 Docker）。

### 9.6 自加 2：`--expect` 取被核对部署的自报值 → `EXIT=0`（§3 的证据）

对 9.1 的篡改镜像传 `--expect` = 它自己烘焙的 `…7864`：`source commit` 仍是
`acf633ff9e71…`，输出 `OK`、`EXIT=0`。即：**当 `--expect` 与被测 bundle 同源时，检查只剩自证**；
「`docker load` 送来另一个提交」这件事挡不挡得住，完全取决于 `--expect` 从哪来（§3 M1）。

## 10. 必须修清单

| 编号 | 内容 | 位置 | 为什么阻塞 |
| --- | --- | --- | --- |
| **M1** | `--expect` 来源口径：写明它应取「要核对提交上的离线重算值」或「构建机/发布记录里的摘要」；取自被核对部署自报值只能证明前端与该 API 同契约，不能证明镜像来自该提交。同时收窄同段「a different commit … therefore fails」这句承诺，并同步 `--help` 示例 | `deploy/README.md:539-546`、`docs/operations/same-version-check.md:77`、`check-image-contract-digest.py:140-145` | 跨机场景下 `--expect` 是唯一路径，当前文案把「配对一致」当成「来源可证」，且与作者自己「label 看不住」的反例自相矛盾；不改等于让本 PR 的核心判据在目标场景里自证 |
| **M2** | 给 `main()` 的候选/相等裁决补表驱动用例（`0→1`、`多→1`、`不等→1`、`相等→0`），或抽成纯函数后测 | `tests/ci/test_image_contract_digest.py`、`check-image-contract-digest.py:176-208` | 本 PR 的产品就是退出码语义；删掉「多候选即失败」后 4/4 全绿（§9.5），等于该判据没有回归保护 |

两处都是文档/测试级改动，不触及已成立的技术实现；改完即可合。

## 11. 可接受但需记录

| 编号 | 事实 | 建议 |
| --- | --- | --- |
| R1 | 生成器缺失时 `load_generator()` 抛未捕获 `FileNotFoundError`：退出码 1（文档口径应为 2），traceback 打印宿主绝对路径（§8） | 把 `exec_module` 包进 `try/except Exception → abort()`（顺带堵住路径外泄）；不阻塞 |
| R2 | `deploy/README.md` 的「95.9 MB unpacked」是 Docker 记账值；实测未压缩层 tar 流 66,159,104 B。`alpine:3.20` 的 `.Size` 同样大于层流和，说明是引擎记账方式 | 改为「Docker 报告 95.9 MB（同引擎记账值）；层压缩后 26.7 MB、`gzip -9` 再省 0.63%」，或直接引实测数字；结论不变 |
| R3 | `50x.html` 按文件名排除：本次正确（它来自 nginx 基础镜像），但若将来 dist 自带同名文件会静默掩盖 | 改成按来源断言（镜像树 = `--target build` 树 ∪ 基础镜像的 `50x.html`） |
| R4 | 多候选失败时只打印候选，不打印 `expected` 是否在其中 | 输出里加一行判断，便于区分「一真一诱饵」和「镜像里没有期望值」 |
| R5 | 扫描只认 `*.js`（`scan()` 的 `rglob("*.js")`）；将来 `.mjs`/HTML 承载摘要会得到「早于守卫」的误导性诊断 | docstring 写明扫描范围；或在零候选时同时报告 guard 标记位置 |
| R6 | admin 没有 `acf633ff` 镜像，本报告的 admin 结论来自本机 `dist` 扫描（1 chunk、1 候选、5 种夹具），非镜像实测 | 发布时把 admin 镜像一并构建并用同一脚本复核 |
| R7 | 镜像 ID 不可由重建复现（同输入三次三个 ID，差异仅为 config `history[31].created_by` 的 Go 指针；12 层 diffID 全等），`save`/`load` 保 ID 成立 | `deploy/README.md` 加半句「ID 只用于传输完整性，不用于判断两台机器构建同版」 |
| R8 | 未复跑作者的 26.08 s 计时（本机 `--target build` 全 CACHED、导出 2.1 s）；构建内容与作者镜像逐字相同（12 层 diffID + 树摘要） | 记录为「计时依赖构建机状态」，不作为判据 |
| R9 | `gzip -9` 结果作者 26,558,151 B / 本机 26,558,167 B（16 B 差来自 gzip 头里的文件名与时间戳） | 无需处理，仅说明复现口径 |

## 12. 只能依赖作者陈述的点

- 26.08 s 的构建计时与「两 `FROM` 解析 0.0 s、零下载」；MegaCube 限速行的历史数据（本机未复验 Linux aarch64）。
- Docker 29.9.3 之外的宿主行为（本报告只在 macOS arm64 + overlayfs 上实测）。
- `docker load` 在**另一台主机**上的实际行为：我只在本机 `load` 同一个 tar（ID 恢复为 `25dd2864…`），
  没有第二台机器可做端到端搬运。
- admin 最终镜像：本机无 `acf633ff` 的 admin 镜像（见 R6）。

## 13. 复现命令（节选）

```bash
# 分叉点口径（本 PR 的 delta）
git diff --stat acf633ff f3a78b15

# 作者镜像身份与 label
docker image inspect "speechnexus/web-workbench:acf633ff9e71f573dbfef5082d0680e2a3aefa41" \
  --format '{{.Id}} {{len .RootFS.Layers}} {{.Size}} {{json .Config.Labels}}'

# 默认路径：烘焙值 vs 本树重算值（EXIT=0 相等）
uv run python scripts/dev-env/check-image-contract-digest.py \
  "speechnexus/web-workbench:acf633ff9e71f573dbfef5082d0680e2a3aefa41"

# 镜像发布树（25 文件）与 14 个 JS 里的 sha256: 字面量
cid=$(docker create "speechnexus/web-workbench:acf633ff9e71f573dbfef5082d0680e2a3aefa41")
docker cp "$cid:/usr/share/nginx/html/." /tmp/r930/html && docker rm --force "$cid"
cd /tmp/r930/html && grep -rEho 'sha256:[0-9a-fA-F]{64}' . | sort | uniq -c
find . -type f ! -name 50x.html -print0 | sort -z | xargs -0 sha256sum | sha256sum   # a13ee6f2…34a5 / 24

# --target build 树摘要（与上面同摘要）
docker build --target build -f deploy/images/web.Dockerfile --build-arg APP_NAME=workbench \
  -t review930/web-dist:check . && docker run --rm --entrypoint sh review930/web-dist:check \
  -c 'cd /workspace/web-dist && find . -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum; find . -type f | wc -l'

# 跨机 tar：层数、层内容、压缩率
docker save "speechnexus/web-workbench:acf633ff…" -o web.tar   # 26,725,888 B
tar -xf web.tar -C blobs && tar -tf web.tar | wc -l            # 23
# 逐层：entries/tops、gzip 解压求和；gzip -9 再压 tar（0.63%）

# 反例：改摘要末位（临时镜像，不碰被保护镜像）
#   docker create → docker cp 改 index-npRtzbgA.js → docker commit review930/tampered-digest
#   再跑脚本：EXIT=1；加 --expect=改后的值：EXIT=0（§9.6）

# 变异：删掉多候选拒绝后跑用例（4 passed，覆盖缺口）
```

所有临时镜像（`review930/*`）与 `/tmp/r930` 证据目录在复核结束后删除；作者的
`acf633ff…` 与 `5cd5a5d0` 两个镜像保留原样。
