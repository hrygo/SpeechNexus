---
title: "A02 私有音频检查回执与消费者交接"
status: review
version: "0.2.0"
date: 2026-10-26
---

# A02 私有音频检查回执与消费者交接

## 目标与当前状态

承接[共享媒体制品](2026-10-26-a02-media-kernel-handoff.md)，交付私有检查成功回执的权威契约、
同源 Python/TypeScript 消费者与 A 的窄 AssetInspector 实现。
实现起点为 `230031043a76ac6c2d04ee6bb2fe93ca38f991c5`，收尾 PR 基于
`54daef6ae4792b69a0e5e8645b09240ff715082e` 提交评审，尚未合入。
后续实施由新负责人重新安排，当前证据和待决事项见[最终交接](2026-10-26-team-a-final-handoff.md)。
关联 [A02 #21](https://github.com/hrygo/SpeechNexus/issues/21)；完整目标见[团队 A Goal](2026-10-23-team-a-goal.md)。

## 交付内容

- [私有成功回执](../../contracts/internal/audio-inspection.schema.json) `1.0.0` 固定
  `schema_version=1`、`recipe_version=pcm-decode-1`、原输入 SHA256/长度及源采样率、声道、样本数、毫秒时长。
  不含路径、PCM、引擎身份、发布许可或停止/释放事实。
- `audio_inspection` 是显式选择的生成 profile；Python 入口为
  `speechnexus_contracts.audio_inspection.parse`，TypeScript 为 `@speechnexus/contracts/audio-inspection`。
  类型/校验器由原生成流水线产出，共享摘要域复用 canonical source，不新增平行字段定义或 HTTP client。
- [ProcessAudioAssetInspector](../../backend/src/speechnexus_backend/speech/infrastructure/audio_asset_inspector.py)
  消费已完整结束的私有进程输出，独立绑定实际输入长度/摘要，检查整数表示、源时钟/时长关系、
  配置时长限制与最后取消状态，再投影既有 `MediaFacts`。
- 仅接 `asr_input` 的 PCM WAV 私有检查；`text_input` 沿用原 TextAssetInspector，
  `voice_reference` 仍不可用。原私有 producer 的显式 PCM 回执有同源类型，但此消费者不接 raw PCM。

默认 AssetRuntime 仍安装 TextAssetInspector。本轮未注册公开音频入口、未修改默认配置、
未写资源/对象/Job/预算，不把可注入消费者写成默认服务已可用。

## 失败与恢复边界

| 输入或状态 | 消费者行为 |
| --- | --- |
| 进程 unavailable/busy/failed/timeout/cancelled/stop_unknown | Inspection unavailable，无事实、无永久内容拒绝 |
| 回执截断、非法 UTF-8、重复 JSON 字段、非有限数、超字节上限、未知字段/版本 | unavailable，不传播原包、vendor 文本或私有路径 |
| 原输入摘要/长度不一致，原生整数表示不符，采样时钟/时长不一致或超配置 | unavailable，不据另一输入的回执生成事实 |
| 已取消、worker 返回后取消、解析/校验期间取消 | 在相应本地边界拒绝事实，保留进程自身停止/占用处理 |
| 合法同源回执且上下文匹配 | 返回源时钟的媒体事实，复位输入流，由上层既有资源事务继续裁决 |

取消检查是本地合作式边界，不是 HTTP 断开传播、物理终止或发布事务的证明。
后续公开装配仍须处理请求取消传递、事务时点重检和 API 重启时的旧进程/未知容量。
回执摘要匹配不认证运行镜像；实际 worker、只读输入、固定命令与原生依赖必须先获得部署资格。
公开内容错误映射、更多容器完整性和正式 HTTP/PG/MinIO 验收留在原 A02 范围。

## 验证与证据

- 末次取消保护的定向集合：56 项通过，包含实际 producer、私有消费者、两次实际受限进程场景，
  以及截断 WAV、错身份/时钟、取消、限额和脱敏。
- “解析期间取消”回归先观察到错误返回 MediaFacts 的 RED，再加最后检查获得 GREEN。
- 完整 Python 契约/资产集合首轮 3167 通过、2 失败：缺统一 profile 样例和 schema 根引用。
  按原门禁补齐 16 组统一正反例及根引用；修复后 3169 通过，补末次取消回归后最终 3170 通过。
- TypeScript 全契约类型检查通过；完整契约测试 1987 通过。原有样例内容保持，新增样例纳入共同集合。
- 33 份源契约校验、生成与漂移检查通过；生成器、锁定版本和公共 HTTP 源不变。
- 全库 Ruff、mypy 853 文件和 8 条依赖边界通过。五包 wheel/sdist 构建成功；
  树外从 backend/contracts/media wheel 加载实际消费者，正确回执和错摘要两例通过，禁止 gateway/controller 导入。

末次取消保护后的完整回归原件保存在 `.runtime/audio-receipt-checks/`；
收尾重验、源码与制品摘要另归档到仓库外稳定证据根，索引见最终交接。初次生成因缺 Node 依赖失败，按仓库已配置的
Node 26.12.1 / pnpm 12.14.0 和 frozen lock 安装后恢复，没有手工修改生成物或升级锁。
本轮未运行模型、数据库/对象存储、公开 SDK/页面或物理拓扑验收。

## 协作与下一出口

已读取 B 及现行 C 会话并完成联合计划审查。C 回执称本机计划已修订为 v1.1.0，
SHA256 `66fccff9c80f0c8c25395a111a19b97cdce3ef13ac620fede3acaaa13b236205`；
仅按回执记录，未取得或独立复核该完整文件，不将其视为本 checkout 已集成内容。

B [#219](https://github.com/hrygo/SpeechNexus/pull/219) 已于 2026-10-26 16:16:13（Asia/Shanghai）
合入 `54daef6ae4792b69a0e5e8645b09240ff715082e`，精确合后 CI `40291095616` 全 14 项成功。
[固定版本交接](https://github.com/hrygo/SpeechNexus/blob/54daef6ae4792b69a0e5e8645b09240ff715082e/docs/architecture/file-tts-binding-handoff.md)
提供固定模型、11 个运行制品、镜像与显式默认，B 报告 187 项定向及 75 项 PG/双库回归。
A 未重跑这些 B 实验、4.52 GB 原件或 GPU；注册输入不创建权威 binding、有效 manifest、资格、已发布声音或默认指针。

- [ ] B 接收收尾 PR 的共享媒体源码映射与兼容入口；A 自测不代签 B。
- [ ] 新负责人指定后续 owner，安排部署镜像、进程恢复/取消传递、公共错误映射及 HTTP/PG/MinIO 接线。
- [ ] A/C 冻结固定 binding 表达、删除/撤销后未知提交边界；A/B 冻结有效指令上限与准入映射。
- [ ] C 在同版公开入口就绪后验证真实 ASR/TTS；不要求等待整包或无关资格全部关闭。

原 ASR/TTS 公开链仍 0/2，F01–F12 完整矩阵未验，真实链关键违规基线未知。
本轮消费者通过不关闭 A02/A00、总 Goal 或 Gate。
