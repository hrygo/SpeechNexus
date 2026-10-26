---
title: "团队 A 收尾与负责人交接"
status: review
version: "1.0.0"
date: 2026-10-26
---

# 团队 A 收尾与负责人交接

用户于 2026-10-26（Asia/Shanghai）要求收敛现有工作、更新文档和 issue、提交最后一个 PR，
后续工作由新负责人重新安排。本轮只交付已经形成的媒体/回执实现和事实交接，不启动新的业务切片、部署或模型实验。
[原 Goal](2026-10-23-team-a-goal.md)和[原下一阶段计划](2026-10-26-team-a-review-and-next-phase.md)
已标记为 superseded；日期、KR、故障矩阵与原完整范围保留为历史要求，不再作为当前派工或排期承诺。

## 交付版本与可消费范围

提取源为 `230031043a76ac6c2d04ee6bb2fe93ca38f991c5`，收尾分支为
`codex/team-a-final-handoff`，已安全吸收 main `54daef6ae4792b69a0e5e8645b09240ff715082e`。
本文件随实现提交 PR 评审；最终 head、CI 和评审状态以 PR 为准，不声称已合并。

| 制品 | 入口 | 当前事实与边界 |
| --- | --- | --- |
| 共享媒体包 | [packages/media](../../packages/media/README.md)、[源码映射](../../packages/media/source-inventory.json) | `speechnexus-media 0.1.0.dev0`；原类型逐字节迁移，decoder 仅换原生类型 import；gateway 原入口导出同一对象。A 自测通过，B 的实际接收仍待 |
| 私有检查命令 | [媒体交接](2026-10-26-a02-media-kernel-handoff.md) | 完整解码并绑定输入摘要/长度，输出有界源时钟事实；PCM WAV/显式 raw PCM，WAV 截断保护；不授予公开 ready |
| 私有成功回执 | [canonical schema](../../contracts/internal/audio-inspection.schema.json) | `audio_inspection` 1.0.0，Python/TS 同源生成；失败/取消/未知不产生成功事实，不包含内容或路径 |
| 窄消费者 | [ProcessAudioAssetInspector](../../backend/src/speechnexus_backend/speech/infrastructure/audio_asset_inspector.py)、[回执交接](2026-10-26-a02-audio-receipt-handoff.md) | 仅消费 asr_input PCM WAV；校验实际输入、完整回执、源时钟、上限与末次取消。text_input 沿用既有实现，voice_reference 不可用 |
| 默认装配 | [检查边界](../architecture/audio-asset-inspection.md) | AssetRuntime 仍为 TextAssetInspector；本 PR 无公开音频 ready、数据库迁移/权限、Job/预算或发布变更 |

B [#219](https://github.com/hrygo/SpeechNexus/pull/219) 已于 2026-10-26 16:16:13 合入上述 main，
[精确合后 CI](https://github.com/hrygo/SpeechNexus/actions/runs/40291095616) 14 项成功。
其固定模型、镜像与完整注册输入已经可获取；尚不是权威注册、声音发布或容量资格。
A 未重跑 B 的模型/GPU/制品原件实验，仍按[固定交接](../architecture/file-tts-binding-handoff.md)的原版本和限制引用。

## 收尾验证与证据保留

同一收尾组合的本地验证结果如下；测试计数不能替代业务或目标部署资格。

| 检查 | 结果 |
| --- | --- |
| Python 契约、资产、窄消费者、真实媒体进程及原 gateway 单元 | 4395 通过，无跳过；含原 3170 项集合和 1225 项 gateway 集合 |
| TypeScript 契约类型检查与测试 | 类型检查通过，1987 测试通过，无跳过 |
| Ruff / mypy / 依赖边界 | 1131 文件格式通过；854 文件类型通过；8 条依赖约束通过 |
| canonical 与生成 | 33 份源契约校验、生成漂移通过；六项兼容/变化检查相对 `54daef6` 通过 |
| 构建与树外消费 | 五包 wheel/sdist 构建；从 wheel 实际解码 4800 样本，正确回执可消费、错摘要拒绝；禁止 gateway/controller 导入，并核对项目模块均从 wheel 加载 |
| 部署配置 | Compose config 通过；此前 Docker COPY 最小上下文 frozen/no-editable 安装通过；未实际构建部署镜像 |

复验入口：

```sh
uv run pytest -q tests/contract tests/unit/test_assets.py tests/unit/test_assets_postgres.py \
  tests/unit/backend/test_audio_asset_inspector.py tests/unit/media tests/unit/gateway --no-cov --tb=short
pnpm --filter @speechnexus/contracts typecheck
pnpm --filter @speechnexus/contracts test
make contracts-check contracts-drift
CONTRACT_BASE_REF=54daef6ae4792b69a0e5e8645b09240ff715082e make contracts-compat \
  contracts-http-changes contracts-speech-changes contracts-coordination-requests \
  contracts-coordination-results contracts-realtime-changes
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run lint-imports
uv build --all-packages --out-dir .runtime/team-a-final-handoff/dist
make stack-config
```

Node 26.12.1 / pnpm 12.14.0 使用仓库锁定依赖；Python 命令使用 uv workspace。
本地原件归档至仓库外 `/workspace/.speech-nexus-team-a-evidence/2026-10-26-final-handoff/`，
有逐文件摘要、源码补丁、验证日志和最终 wheel，仓库内[证据摘要](../audits/2026-10-26-team-a-closeout-evidence.json)
保留命令、结果和制品摘要。该本机目录不是 B/C 可下载地址；远端以 PR 源码和 CI 为可获取入口，可按命令重建。

首次统一契约回归的缺样例/根引用失败、解析期间取消的 RED、初次生成缺 Node 依赖等原记录保留，
均已修复并回归。旧版本 ENOSPC/覆盖失败、取消的 CI 与清理未知仍在原 issue/历史证据中，不由本轮结果改写。
本轮未执行数据库/MinIO 联验、真实模型、公开 SDK/页面或物理拓扑验收。

## 未交付能力与待决事项

| 待交接项 | 责任边界 | 接收或解除条件 |
| --- | --- | --- |
| 媒体包兼容与部署资格 | A 打包；B codec；新负责人指定集成维护者 | B 消费本 PR 固定源码或构建 wheel；核对异常/类型身份和格式边界；固定部署镜像/原生依赖及容量、旧进程恢复 |
| 音频 Asset 公开接线 | A02/A04 | 冻结公共错误/投影、取消传递和事务重检，正式 HTTP/隔离 PG/MinIO 证明 ready、原键、固定对象、TTL 与恢复 |
| 权威配置、业务 Job 与发布 | A；B 提供执行/资格事实 | 有效 binding/defaults/适用 VoiceRevision、完整原子准入及 Job/outbox、Artifact/receipt/公开下载；注册输入和健康检查不代替这些事实 |
| 固定 binding 与未知提交 | A/C | 不在公开请求自加 binding_revision_id；明确固定资源表达、受理前置条件、完整 Job/有效配置校验及删除/撤销后未知重放边界。无键不承诺恢复，不隐含按键查询接口 |
| CustomVoice 参数映射 | A/B；C 消费 | 首轮一次 intent ≤400 codepoints，pronunciation=null；Nexus instructions 2000、标准接口/内部命令 4096、引擎默认 500 分别记录。冻结模板实际 argv、2001–4096 准入/拒绝，不静默截断 |
| 联合验收与运行窗口 | 新负责人协调 A/B/C/运维 | 指定 owner、同版公开入口、隔离 PG/MinIO/最小角色和实际窗口；三方各签自身证据。当前没有新增 GPU 或联合环境时间承诺 |

C 已回执其本机计划 v1.1.0 采纳联合审查，摘要为
`66fccff9c80f0c8c25395a111a19b97cdce3ef13ac620fede3acaaa13b236205`；
A 未独立复核该完整文件，不假定其已提交或存在于本 checkout。
C-N02 的恢复工作不阻塞独立 C02；ASR profile 可选，builtin TTS 不等待完整设计/克隆资格。
公开 Job 没有 unknown 状态；应用未决、请求取消、单请求释放、整代停止、lease 和 base 分别留证。

## 清理及状态结算

- 本轮所有既存候选纳入一个收尾 PR；当前树只有一个 worktree，不清除其他团队分支或运行资源。
- 日志、wheel 和实验原件先归档；模型、数据库、对象、旧失败证据及未知资源均不删除，不做无归属清理。
- #19/#21/#23 和总控 #6 回填交接事实；未满足的任务、AC 与 Gate 保持开放，不用本 PR 自动关闭父包。
- 真实公开链仍 **0/2**，F01–F12 完整矩阵未验，关键不变量的真实链基线未知；原 Goal 不标记成功。
- 本次 PR 提交后结束本轮新增实施；评审、集成及后续工作由新负责人重新决定，不沿原时间盒自动续作。
