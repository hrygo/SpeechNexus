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
