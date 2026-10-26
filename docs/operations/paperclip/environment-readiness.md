---
title: SpeechNexus agent 环境准备与验收
status: blocked
version: "1.2.0"
date: 2026-10-26
---

# SpeechNexus agent 环境准备与验收

用户当前要求先准备环境，不派发真实业务任务。七个 agent 保持暂停；本轮后续只执行管理员的工具、依赖、合成媒体、隔离服务和适配器传输检查。
工作流与分工见 [Paperclip 工作流](../paperclip.md)。部署内的准确路径、配置快照、凭据引用、原始检查和回退记录保存在 Paperclip **DEL-36**，不写入公开仓库。

原始连接与适配器传输证据固定在 `54daef6ae4792b69a0e5e8645b09240ff715082e`。
收尾时主干前进到 `fe4a825ed025b7ebca3aa3d577bcb54bc9ea8de7`；三机另外建立该版本的独立验证 checkout，完成 frozen / offline 安装、全部五个 Python workspace 包导入与合成媒体检查，后续默认源码基线也已更新。
这里的环境证据不替代后续任务自己的受检 SHA，也不表示公开 ASR/TTS、真实引擎、物理集群或任何产品 AC 通过。

## 当前结果

2026-10-26 核验：三机工具链与锁定依赖、两台 Mac 的浏览器与合成媒体、MegaCube 的隔离数据库/对象存储均已准备并完成相应自检。
七个岗位均取得过所配置模型的成功连接与 `hello` 结果。两台远端的隔离源码传递和工作区修改回传通过。

两条历史未计价记录已依据用户确认，通过线上 r6 既有审计调整接口更正为有依据的零增量费用。2026-10-26 15:45 UTC 回读：未计价数为 0，预算状态 `ok`，原 incident 已自动解决；原始用量、token、回执与摘要均保留，原预算策略未改变。七岗仍手动暂停。

**尚未放行：** A/B 环境保存存在已复现的程序缺陷，修复见 [Delegra PR #20](https://github.com/hrygo/Delegra/pull/20)，最终检查和发布尚未完成。自动识别以后订阅内用量是另一项能力扩展，[PR #21](https://github.com/hrygo/Delegra/pull/21) 已退回草稿，本次不部署，也不是更正这两条历史记录的前置。完整 agent 任务准入、工具调用、报告回写与收尾仍为 `not_run`；管理员检查不能替代这一层。
PR #20 的最终源码 `37430cd23efef914b5556f84b4456836d54e7785` 已通过本地全仓类型检查、构建和独立源审；15:59 UTC 核验 50 项 CI 成功、1 项跳过。本地全量长测仍在运行，不计为已通过；线上仍为 r6，A/B 拟更新字段尚未保存。
用户的暂缓业务派工要求继续有效。远端 Git commit 回收另有明确能力限制，见下文。

| 岗位 | 环境与 Harness | 已有环境证据 |
| --- | --- | --- |
| 项目经理 | 本机 / Paperclip Runner Codex | 模型连接及 hello；任务分配权限已单独配置，保持暂停 |
| A 契约与集成 | 本机 / Paperclip Runner Codex | 模型连接及 hello；Python / Web 锁定依赖、媒体与浏览器自检 |
| B 执行与调度 | MegaCube / Codex CLI | 模型连接及 hello；固定源码、修改回传、Python / Web 依赖、隔离服务读写 |
| C 工作台 | 本机 / Claude Code | 原 DeepSeek 接入与 hello；Web / 浏览器及媒体工具 |
| 独立验收 | MacBook / Codex CLI | 原 DeepSeek 接入与 hello；独立源码、修改回传、Web / 浏览器及媒体工具 |
| 交叉审查 | 本机 / Claude Code | 原 MiMo 接入与 hello；与本机实现岗位共用版本固定的工具链，各任务使用独立 checkout |
| 证据与文档 | 本机 / OpenCode | 专用 OpenCode 1.18.34、原 Sage 接入及 hello；原应用与全局入口未改写 |

## 工具与依赖

| 项目要求 | 准备结果 |
| --- | --- |
| `.python-version` | 三机 Python **3.14.9**，由专用 uv 管理 |
| `.nvmrc` | 三机 Node **26.12.1** |
| `package.json` | 三机 pnpm **12.14.0** |
| uv | 三机 **0.13.5**，使用项目专用安装目录及缓存 |
| Python workspace | 三机执行 `uv sync --locked --all-packages --all-groups`；新主干锁定 113 packages，新增共享媒体包及各服务包导入通过；专用缓存支持 offline 安装 |
| Web workspace | 三机执行 `pnpm install --frozen-lockfile`；另准备独立的 pnpm store，并完成 frozen / offline 重建 |
| 浏览器 | 本机与 MacBook 的 Playwright **1.64.1**、Chromium **156.0.8078.4**；实际启动无头浏览器并定位合成页面按钮 |
| 媒体 | 三机 PyAV **19.0.2**、soundfile **0.14.0**；实际编码并解码 1600 个静音样本；执行 wheel 内 FFmpeg |
| 远端 Codex CLI | 两机独立固定 **0.164.0**，不依赖用户 `$HOME` 下的入口 wrapper |
| 基础命令 | Git、Bash、curl、tar、make、jq；MacBook 补充官方 ripgrep **15.2.0**，核验发布制品 SHA256 |

Node / uv 的新增发布包核对官方校验值；npm 安装使用精确版本与包完整性记录。OpenCode 的官方安装脚本经读取后执行，解决包管理器默认未运行安装脚本的问题。
工具安装位于仓库外的项目专用目录；没有改写全局 Node / pnpm / uv 入口、Heluo 工具链或用户应用。
两台 Mac 的激活文件显式指定 Playwright 浏览器缓存路径，已按该路径启动 Chromium，避免 Harness 的受管 home 改变默认缓存位置。

每个新工作区仍须建立自己的 `.venv` 与 `node_modules`。缓存可复用，不能共享带旧源码路径的可编辑安装、直接链接别的 checkout 的依赖目录，或假定包缓存等于当前工作区安装完成。
管理员准备完受检工作区后，执行者先激活该主机的项目环境，再核对版本和依赖；缺失时先准备，再验收。任务提供 `SPEECHNEXUS_TOOLCHAIN_ENV` 的准确部署内路径。

```bash
source "$SPEECHNEXUS_TOOLCHAIN_ENV"
uv --version
node --version
pnpm --version
uv sync --locked --offline --all-packages --all-groups
pnpm install --offline --frozen-lockfile
```

这些命令要求使用部署中已配置的专用工具链与缓存。版本、锁文件或平台变化后重新做必要的准备；离线缺包应报告缺口，不能去掉锁定参数。

A/B 的新增缓存/端点字段仍保留原已保存值。已通过隔离 PostgreSQL 回归确认两项根因：JSONB 字段顺序被误判为连接变化，保存路径又把未解析的环境绑定交给校验器。修复统一了身份比较和有效环境解析，保留真实模型、Harness、连接及执行环境变化的必要校验；执行目标不可用时只读绑定元数据，不解密秘密。修复候选尚未部署，不能把代码检查通过报告为保存成功。
当前以已实测的环境激活脚本提供 pnpm store，MegaCube 的激活脚本同时选择测试端点 `59002`。B 的已保存默认端点仍为原 `59000`，不能忽略激活步骤或把拟更新值报告为已落库。
完整任务闭环须核对有效环境；平台保存校验问题保留在 DEL-36，不关闭或绕过校验。

## 隔离服务与凭据

正式数据库验证集中在 MegaCube。两台 Mac 不复用本机共享 PostgreSQL，也不为准备环境另建产品数据库。

- PostgreSQL **16.15**：现有项目隔离实例，loopback **55432**；新建唯一临时 schema、写入/读取及删除自身 schema 通过。
- MySQL **8.4.8**：现有项目协调测试实例，loopback **55434**；新建唯一临时 database、InnoDB 写入/读取及删除自身 database 通过。它只提供协调路径的补充验证。
- MinIO：既有 loopback **59000** 服务的独立合成桶读写与清理通过；Paperclip 集成另准备 loopback **59002** 专属测试容器，避免仓库测试管理脚本与既有开发服务争用端口。

Paperclip 专属 MinIO 使用当前受检配方的已构建镜像，核对配方摘要及实际 image ID；运行用户 `10001:10001`，内存上限 **512 MiB**、**1 CPU**、数据为 **256 MiB tmpfs**，仅监听 loopback。
不挂载宿主业务内容或 Docker socket，不替换已有开发/生产容器。该临时存储只保存合成测试数据；服务未运行时须先由获准操作者准备，健康检查不能替代对象操作。

B 通过 Paperclip 的加密 secret store 与 `secret_ref` 取得测试连接，凭据不进入仓库、角色正文、任务报告、命令参数或其他岗位配置。
已有服务时直接执行验证指南对应的用例；禁止重新运行整机 bootstrap 或通过更换端点字符串冒充已处理的服务所有权。

## 源码、修改与交付

项目仓库关联已纠正为 `hrygo/SpeechNexus`，默认采用平台受管 repo-only 源和任务独立工作区。
七个 agent 已加入当前私有项目；项目仍保持私有。新任务分支采用 `codex/paperclip-{{issue.identifier}}-{{slug}}`。

Paperclip Runner 的 SSH 路径在本轮探针中只给出远端空目录。因此 B / QA 显式配置为 Codex CLI 的 Legacy runner，保留原模型与 AI 连接，且未开启 `dangerouslyBypassApprovalsAndSandbox`。
独立验证 checkout 只供命令和连接自检；实际执行必须使用平台传入的任务 checkout，不能在那里开展业务修改。

管理员调用已安装版本的适配器传输组件，在两台远端分别验证了：

1. 固定 SpeechNexus 完整 SHA 的源码到达专属运行目录，目录具备 Git 仓库且不配置 Git remote。
2. 已跟踪文件的合成修改和一个合成新文件正确回传到专属宿主工作区。
3. 回传后 base SHA 不变，宿主索引保持原状态，工作树只出现预期修改。

**当前工作流采用上述修改回传模式。** 远端不 commit / push；本机经理对回传修改和证据审查后提交、建立 PR。QA 仍须对最终受检提交复核。

另一个 MegaCube 探针验证了远端新提交可到达宿主，但宿主索引仍保留旧状态，同一合成文件显示 staged deletion 与 untracked 两项。
这一模式未通过验收，复现场景与提交保留在部署内；不能把它描述为干净的提交回收。平台修复并重新检查前，不开放远端 commit。

## 调度、预算与启用门禁

全部 agent 保持 `paused`；timer 关闭、按需唤醒、单 agent 并发 **1**，配置好的队列不会因环境文档更新而自动执行。
项目经理拥有任务分配权限，不增加 agent 创建或技能创建权限。本轮没有派发业务开发、模型下载/加载、真实音频或生产运行任务。

保留原项目 **10000 billed cents lifetime** 上限、80% 提醒、hard stop 与 `unpricedUsagePolicy=block`。
两条历史记录已通过既有接口分别追加带用户确认依据、版本与幂等键的金额调整；金额仍为零，状态从 `unpriced` 变为 `reported`，原计费类型和原始证据保持不变。既有预算机制自动重新判定为 `ok`，没有提高额度、关闭未计价阻止或解除 agent 手动暂停。用户确认的“订阅已包含”仅表示相关运行不再加收按量费用，不表示订阅购买价为零，也不推广到其他连接或以后运行。未来 API-key 用量的自动订阅声明仍是未部署的候选功能；无价格依据的新记录仍按原策略处理。

后续启用顺序：

1. 完成环境保存修复的检查、独立审查及发布门禁；部署后保存并回读 A/B 最终字段，复核预算与七岗暂停。历史费用更正已完成，不依赖新计费功能。
2. 先验收非业务的任务准入、实际 checkout / 工具访问、任务文档回写与 workspace finalize，不能用本次管理员自检替代。
3. 核对角色正文、服务就绪、受检版本和用户当前派工要求，再由经理按工作流放行明确的切片。

DEL-37–DEL-40 保留早期环境探针的失败与未验事实；不将旧探针改为成功。新配置下尚未执行的完整任务闭环记为 `not_run`。
