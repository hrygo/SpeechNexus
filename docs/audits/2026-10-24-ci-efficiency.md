---
title: CI 效率调研与实施证据
status: active
date: 2026-10-24
---

# CI 效率调研与实施证据

## 基线与瓶颈

基线为 [PR #46 CI](https://github.com/hrygo/SpeechNexus/actions/runs/39988924348)，
提交 `b0806b518749513e555d6a5d11c9c194d6d9fdab`。取 GitHub Jobs API 的
`started_at/completed_at`，不把排队时间混入执行耗时。

| 项目                              | 基线耗时 |
| --------------------------------- | -------: |
| Python 必需检查                   |   353 秒 |
| OpenAI 契约兼容检查               |   153 秒 |
| 初始化 PG/MySQL 服务              |    42 秒 |
| Python 测试与覆盖率               |    62 秒 |
| PG/InnoDB 集成测试                |    58 秒 |
| Python 依赖安装                   |     2 秒 |
| Node 契约工具安装（含 pnpm 激活） |     6 秒 |
| Web 完整检查                      |    31 秒 |

主耗时来自不必要的契约展开与串行路径，依赖下载不是本次主要瓶颈。

## 最佳实践取舍

- 根据实测优化关键路径：OpenAI gate 只比较原本声明的标准路径，先筛选后
  展开引用；保留完整文档作为引用上下文。每次比较单独缓存基线/当前引用文件，
  避免重复 `git show` 和 YAML 解析，不使用可能掩盖后续编辑的全局缓存。
- 独立工作并行：Python 静态/非集成测试与隔离双数据库服务/测试分为两个 job。
  保留原 `Python quality and tests` 必需检查名作为汇总；仅两者均 success 时通过。
  依赖失败/取消/跳过时也执行汇总并失败，避免默认跳过依赖链误放行。
  依据 [GitHub needs/if 语义](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#jobsjob_idneeds)。
- 保留 `uv.lock` 缓存和 CI prune。当前安装仅 2 秒，未添加 `.venv` 打包缓存；
  uv 官方建议使用自己的缓存与 CI prune，见
  [uv Actions 指南](https://docs.astral.sh/uv/guides/integration/github/#caching)。
- pnpm 仍采用固定版本与 frozen lockfile 安装。本次安装仅数秒，先不引入额外
  cache action。后续若下载成为瓶颈，使用 lockfile 作为键的 pnpm store 缓存；
  不缓存 `node_modules`。参照
  [setup-node 官方说明](https://github.com/actions/setup-node#caching-global-packages-data)。
- checkout 改为浅克隆，PR 仅另取当前目标分支的单个提交；保留完整 baseline 文件树，
  兼容检查仍真实比较目标分支，未以“无变更”短路。
- 删除独立重复的 token lint 步骤；`pnpm lint` 本身继续执行相同门禁。
- 保留所有验证、双 DSN 检查、锁定依赖、SHA 固定 actions、覆盖率阈值、只读权限、
  过时 run 取消及 main push 验证。未添加 workflow path filter，避免必需检查保持
  Pending，见 [GitHub 路径过滤说明](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onpushpull_requestpull_request_targetpathspaths-ignore)。

## 验证

- 本地原有 52 项兼容回归及新增 3 项性能/引用回归通过：不展开 Nexus-only 路径；
  仍识别标准路径引用的本地组件变化；每次比较每侧文件只读一次，下次比较能识别
  新的共享 schema 收紧。
- 实际汇总 shell 的 16 种 success/failure/skipped/cancelled 组合全部验证，
  只有双 success 通过。
- 同一完整 CLI 的本地三次测量：1.061、1.050、1.116 秒，中位 1.061 秒。
  本地与 GitHub runner 硬件不同，这不是 CI 的同环境前后倍数。
- actionlint 1.7.12、Ruff、mypy、import-linter 与生成漂移检查通过。

## 实施后测量

优化由 [PR #47](https://github.com/hrygo/SpeechNexus/pull/47) 合并，main 提交
`b3e0ed340763b827333747d464b68fd7f4981116`。最终 PR head
`8aa2b197de45cc647c306c8cadfa3f5af34bf0c7` 的
[CI run 39990005346](https://github.com/hrygo/SpeechNexus/actions/runs/39990005346)
五项检查全部通过。以下仍使用 Jobs API 的实际执行时间；关键路径从最早 job 开始
到最后必需检查完成，包含汇总 job 调度间隔，不含 workflow 开始前的排队时间。

| 项目 | 优化前 | 优化后 |
| --- | ---: | ---: |
| 必需检查执行关键路径 | 353 秒 | 140 秒 |
| OpenAI 契约兼容步骤 | 153 秒 | 1 秒 |
| Python 静态/非集成 job | 原先串入单一 Python job | 134 秒 |
| PG/InnoDB job | 原先串入单一 Python job | 112 秒 |
| Web job | 31 秒 | 27 秒 |

关键路径缩短约 60.3%。这是同一轮优化保留完整保护范围后的两次实际运行对比，
不是长期分位数，也不是后续版本的固定 SLA。新增的回归用例同样通过；覆盖率
阈值、双数据库、契约漂移与失败/跳过不能放行的汇总规则均保留。

复核入口（只读取元数据，不下载日志或接触业务内容）：

```sh
gh api repos/hrygo/SpeechNexus/actions/runs/39988924348/jobs
gh api repos/hrygo/SpeechNexus/actions/runs/39990005346/jobs
```

后续 Asset、gateway 和恢复测试持续增加，不能将其总耗时直接与本表混算优化幅度。
例如 [#61 CI](https://github.com/hrygo/SpeechNexus/actions/runs/40026575924)
的存储 job 已增至 186 秒；它包含新增的真实 PG/版本化 MinIO 与 HTTP/维护验证。
再次优化应固定测试集合并重新取 Jobs/Steps 基线，不用跳过测试换取较短时间。

单次测量受 runner、镜像和缓存命中影响。数据库并行增加一次 Python 安装和一个
轻量汇总 job，用执行分钟换取更短反馈；本次不宣称长期 runner 成本或全部
运行分位数同步下降。当前依赖安装只需数秒，继续保留简单的锁文件/uv 缓存方案；
下载成为可测瓶颈后，再评估 pnpm store 缓存或镜像构建缓存。

## 第二轮：契约测试的解析开销

文件/内部协议样例增多后，重新测量发现测试 helper 每次都用纯 Python
`yaml.SafeLoader` 解析整份文档。这里需要每次新读以保护变更检测和可变测试副本，
因此本轮不加入文档缓存，改用可选 `CSafeLoader`，缺少 LibYAML 扩展时回退
`SafeLoader`。两者都使用安全构造器；不使用 `Loader` / `CLoader`。

[PyYAML 官方说明](https://pyyaml.org/wiki/PyYAMLDocumentation)介绍了 LibYAML
解析器的性能，也指出两种解析器存在细微差异。因此增加整个仓库契约语料的递归
类型/值对照，以及 Python 对象标签拒绝、无扩展回退、变更后重新读取和调用方
修改不泄漏的回归。本轮只改测试加载路径；生成器、兼容 CLI、产品配置与校验规则不变。

同一本机、同一份 `speech-openapi.yaml` 的三次测量中位数为：SafeLoader
0.1673 秒，CSafeLoader 0.0215 秒。两个未改用例文件组成的同一测试集合：

```sh
uv run pytest tests/contract/test_consumer_bindings.py \
  tests/contract/test_generated_bindings.py -q --no-cov
```

121 项全部通过，修改前 15.74 秒、修改后 3.56 秒，耗时减少约 77.4%。
新增 33 项解析语义/安全/回退验证也全部通过。这是同机局部测试集合测量，
不是整体 CI 加速承诺；远端完整检查结果和耗时以本次 PR 对应 Actions run 为准。
继续保留原有用例、覆盖率和数据库门禁，不减少测试集合。

## 第三轮：完整集成集合分片

随着真实协调用例增加，[PR #88 CI](https://github.com/hrygo/SpeechNexus/actions/runs/40071291404)
的双库作业在06:42:57–06:51:08 UTC执行，耗时491秒，接近原10分钟上限。
同轮Python静态/单元143秒、Web28秒；本轮优化对象是双库关键路径。
该源码基线为`eb299f7a6a14868be77adb09c70c0ad19a6a9987`，完整集成904项：
本地888 passed、16个既有PG-only参数跳过另计；不能与早期186秒的小集合混算。

采用两个独立GitHub runner，各自启动相同PG16.15、InnoDB8.4.8及版本化MinIO。
每个runner先收集同一完整`-m integration tests/integration`集合，再按
`SHA256(nodeid) % 2`稳定分配完整用例。基线实际分为452/452项，完整集合摘要为
`sha256:3c0bb1ddc88dcc735b63568de6a8c304a0fb7f139eff69a5fef14e93a4f91da6`。
没有维护可能漏掉新增文件的白名单，也没有减少数据库参数或拆散单个竞争测试的参与者。

取舍依据：

- [GitHub matrix指南](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/run-job-variations)
  说明独立作业、制品汇总、fail-fast和max-parallel；本轮`max-parallel: 2`、
  `fail-fast: false`，一份失败仍收集另一份证据，不设continue-on-error。
- 保留原五个检查名。`PostgreSQL and InnoDB integration`现在依赖两个分片，
  仅matrix结果success且完整证据核验通过才success；失败/取消/跳过也执行汇总并失败。
  原`Python quality and tests`继续要求Python静态/单元和该双库汇总均成功。
- 使用官方upload-artifact v7.0.2和download-artifact v8.0.2，分别固定SHA
  `cf430e030ddbb5b0abf93d22962f4752f3646cd9` /
  `9000827ccba6bdab643e8b6fd33ac0654aef8333`；两者使用Node24。
  [下载action参数](https://github.com/actions/download-artifact/blob/9000827ccba6bdab643e8b6fd33ac0654aef8333/action.yml)
  明确支持当前run的pattern/merge-multiple及摘要不符时报错。
- 报告使用每分片唯一文件名，只含测试身份/结果/摘要/耗时，不含凭据、DSN、正文或捕获输出。
  上传缺文件即失败，保留7天；同run重跑同分片覆盖其旧报告，成功分片可复用同源码证据。
  聚合只下载当前repo/run，要求相同checkout SHA，不能混合另一提交。
- 汇总脚本只用stdlib，在runner系统Python3.12执行，不增加uv/依赖安装；新增制品步骤
  是完整性核验的成本，不宣称减少总runner分钟或服务启动成本。

### 执行证据不能由数量替代

`scripts/dev-env/integration-shards.py`在pytest完成后记录完整/选中nodeids、逐用例结果、
退出码与实际执行标志。聚合核对报告闭合形状、版本、source SHA、分片身份、完整集合摘要、
相同收集结果、稳定选中集合、互斥/并集，以及每个选中用例都有成功执行结果。
缺失/重复报告、错误SHA、不同/不完整集合、失败/取消/xfail、仅收集、缺失结果均不能通过。

只允许现存`test_gateway_submit_ledger.py`的`[mysql]`参数报告为不适用跳过，当前16项，
在step summary单列且不计通过；其他集成用例跳过会失败。原测试的skip条件没有修改。
runner固定完整pytest入口，拒绝`PYTEST_ADDOPTS`及-k/-m缩窄、ignore、deselect、--lf等筛选。
原`make coordination-test` / `make integration-test`保持不变，仍可运行完整未分片门禁。

新增32项专项包含临时git仓库中的真实pytest运行/失败/collect-only，以及遗漏/重复/篡改、
不完整结果和失败/取消/跳过汇总守卫。checker用`python -I -S`验证无需site packages，
并验证Python3.12语法兼容。业务代码、集成测试和SQL/迁移/权限均未修改。

本机按同一隔离服务顺序验证两个分片：各444 passed / 8 skipped，pytest耗时201.33秒和192.93秒；聚合核对完整904项并集、888 passed / 16既有跳过。新runner报告最长执行201.44秒（含收集/收尾）；这不是GitHub并行加速实测。
远端两个runner的实际作业耗时、保护检查和完整性核验结果，记录在
[A00 #19](https://github.com/hrygo/SpeechNexus/issues/19)本轮交付块及其Actions链接。
比较时同时报告关键路径与并行增加的服务/runner成本，不用本地时间推导远端倍数。

## 第五轮：慢运行器方差与 6 片分区

四片并行（第四轮）生效起（含该 PR 自身的验证运行与合并后的 `main` push），连续 10 次运行全部
success，整轮落在 175–214 秒（约 2 分 55 秒–3 分 34 秒），没有再出现优化前 4–8 分钟的整轮：

| run | 提交 | 事件/分支 | 整轮 |
| --- | --- | --- | ---: |
| [203](https://github.com/hrygo/SpeechNexus/actions/runs/40076497465) | `14541411` | PR `codex/ci-shard-4` | 175 秒 |
| [204](https://github.com/hrygo/SpeechNexus/actions/runs/40076807703) | `5ce396ae` | push `main` | 191 秒 |
| [205](https://github.com/hrygo/SpeechNexus/actions/runs/40076893032) | `199a7a48` | PR `feat/a00-workspace-read-contracts` | 210 秒 |
| [206](https://github.com/hrygo/SpeechNexus/actions/runs/40077135439) | `2a46c6e2` | push `main` | 198 秒 |
| [208](https://github.com/hrygo/SpeechNexus/actions/runs/40077836265) | `5a60da7e` | PR `feat/a04-workspace-read-api` | 202 秒 |
| [209](https://github.com/hrygo/SpeechNexus/actions/runs/40078130260) | `4b08c396` | push `main` | 196 秒 |
| [210](https://github.com/hrygo/SpeechNexus/actions/runs/40080023597) | `d67b13bd` | PR `task/b-b03-tts-adapter` | 211 秒 |
| [211](https://github.com/hrygo/SpeechNexus/actions/runs/40080382232) | `d683d31d` | PR `task/b-b03-tts-adapter` | 197 秒 |
| [212](https://github.com/hrygo/SpeechNexus/actions/runs/40080701926) | `32c2bf1f` | push `main` | 198 秒 |
| [213](https://github.com/hrygo/SpeechNexus/actions/runs/40082831951) | `99d47cdd` | PR `task/b-b02-controller-reconcile` | 214 秒 |

列表之后的一次 [run 40083273624](https://github.com/hrygo/SpeechNexus/actions/runs/40083273624)
整轮 317 秒（5 分 17 秒），越过 5 分钟预算。原因不是测试集合变化，而是一台运行器整体变慢：

| 观测 | 该运行 | 同期正常运行 |
| --- | ---: | ---: |
| `Initialize containers`（分片 1） | 57 秒 | 30–42 秒 |
| 对象存储构建与启动（分片 1） | 16 秒 | 8–16 秒 |
| pytest 分片 1 | 216 秒 | 104–128 秒 |
| pytest 分片 0 / 2 / 3（同一次运行） | 118 / 122 / 87 秒 | — |
| 整轮 | 317 秒 | 175–214 秒 |

同一次运行里其余分片仍在正常区间，分片 1 的用例数（224）也与其它分片接近，所以多出的时间是该
运行器的 CPU/磁盘/网络，而不是分区失衡。稳定哈希分区对*用例数*均衡，对*用例耗时*不均衡：
只要最慢的那台运行器整体变慢约 1.7 倍，四片配置的整轮就会超过 5 分钟。

因此本轮把稳定哈希分区从 4 片改为 6 片（`max-parallel: 6`、`shard: [0…5]`、`--count 6`），
`fail-fast: false`、双库服务、完整性核验、既有跳过规则与分片证据全部不变。同一份完整集合
（摘要 `sha256:8690ba6cc9329d67404852f971769f9676151e1321acb003e2a8f76479d83dca`，914 项）
按 `SHA256(nodeid) % 6` 重新分区，互斥且并集完整：

| 分区 | 每片用例数 |
| --- | --- |
| 4 片 | 217 / 224 / 237 / 236 |
| 6 片 | 139 / 144 / 168 / 150 / 147 / 166 |

关键路径与预算归属：最慢分片作业 ≈ 作业内固定开销（`Initialize containers` 30–57 秒、对象存储
8–16 秒、检出与依赖约 8 秒）+ 该片 pytest 时间，再加双库完整性汇总（5–9 秒）与
`Python quality and tests`（3 秒）。四片时单片 pytest 104–128 秒，慢运行器上放大到 216 秒；
六片把同一批用例切到约 2/3，即使再遇到 1.7 倍慢运行器，预计整轮仍落在 4 分钟以内。代价是每次
运行 6 个运行器、每个仍自建 PG 16.15 / InnoDB 8.4.8 / 版本化 MinIO，服务启动总量高于四片；
本轮优先保证 5 分钟反馈目标。

观测与触发下一档调整：聚合脚本已经把 `Longest shard execution …s` 写进步摘要，分片变慢会先在
运行摘要可见。触发条件（任一）：单分片 pytest 步骤超过 3 分钟、整轮超过 4 分钟，或再次出现整轮
被单台慢运行器拉过 4 分半。届时优先降低单分片用例成本（例如减少每个用例的建库/迁移开销）而不是
继续加片；仍然不通过减少测试集合、放宽双库/完整性核验、跳过检查或路径过滤来提速。

## 第四轮：四片并行把整轮压到三分半

双库分片（PR #90）之后，[PR #92 首轮 CI](https://github.com/hrygo/SpeechNexus/actions/runs/40075633811)
的执行路径为 07:32:39–07:37:37 UTC，整轮 4 分 58 秒，距 5 分钟目标只剩 2 秒余量，
也逼近 `integration_shards` 的 10 分钟超时。两个分片各自的 pytest 步骤接近 3 分半，
关键路径由分片本身决定，继续留在 2 片没有余量。本轮把稳定哈希分区从 2 片改为 4 片，
`max-parallel: 4`、`fail-fast: false` 和其他完整性保护不变。

同一份 904 项完整集成集合（摘要
`sha256:3c0bb1ddc88dcc735b63568de6a8c304a0fb7f139eff69a5fef14e93a4f91da6`）
在本地按 `SHA256(nodeid) % 4` 重新分区，四片分别为 216/220/236/232，互斥且并集完整。
没有新增白名单，也没有把竞争测试的参与者拆开。

绿色实测为 [run 40076065728](https://github.com/hrygo/SpeechNexus/actions/runs/40076065728)，
提交 `6c9319f`，07:55:56–07:59:27 UTC，整轮 **3 分 31 秒**：

| 作业 | 起止（UTC） | 耗时 |
| --- | --- | ---: |
| Web 质量/测试/构建 | 07:55:59–07:56:31 | 32 秒 |
| Python 静态检查与单元测试 | 07:55:59–07:58:33 | 154 秒 |
| PG/InnoDB 分片 0 | 07:55:58–07:58:41 | 163 秒 |
| PG/InnoDB 分片 1 | 07:55:58–07:58:43 | 165 秒 |
| PG/InnoDB 分片 2 | 07:55:58–07:58:51 | 173 秒 |
| PG/InnoDB 分片 3 | 07:55:59–07:59:12 | 194 秒 |
| 双库完整性汇总 | 07:59:14–07:59:23 | 9 秒 |
| 部署配置 | 07:55:58–07:56:03 | 5 秒 |

关键路径是最慢分片 194 秒，比双片时的整轮 298 秒缩短约 35%。四个分片作业
07:55:57–07:55:59 全部启动，没有出现 runner 排队导致的路径拉长；最慢分片多出的
约 20 秒来自初始化容器与拉取对象存储镜像，而不是测试数量差异。

首轮 40075633811 的整轮时间只有 3 分 18 秒，但 `python_checks` 失败：单元测试仍断言
旧的 2 片矩阵（`max-parallel: 2`、`shard: [0, 1]`）。该断言是保护门禁契约的一部分，
因此在 `6c9319f` 同步更新为 4 片；首轮只能作为耗时参考，绿色证据以上表的成功运行为准。

取舍：四片意味着 4 个 runner，每个仍然启动自己的 PG 16.15、MySQL 8.4.8 和版本化
MinIO，服务启动成本高于 2 片。本轮以 2 秒安全余量换取约 1.5 分钟的关键路径余量，
优先满足 5 分钟反馈目标；`fail-fast: false` 保持失败时仍收集全部分片证据。
如果后续需要回收 runner 分钟，应先降低单分片测试耗时或将服务初始化改为共享，
不能通过减少测试集合或放宽完整性核验来提速。
