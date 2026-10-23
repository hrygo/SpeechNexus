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

远端新 CI 的总耗时与各阶段数据在实际完成后补充；单次测量会受 runner、镜像和
缓存命中影响，不代表长期分位数。数据库并行增加一次 Python 安装和一个轻量汇总
job，用总执行分钟换取更短反馈时间；不宣称其同时降低所有 runner 分钟。
