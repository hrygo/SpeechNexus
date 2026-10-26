---
title: "团队 A 完整工作交接与项目接管入口"
status: active
version: "1.2.0"
date: 2026-10-26
---

# 团队 A 完整工作交接与项目接管入口

本文件保留 [PR #221](https://github.com/hrygo/SpeechNexus/pull/221) 原交接时点的完整记录，覆盖 A00–A07、
媒体实现、三队接口、全部 issue 状态、证据与资源保留、风险及接管步骤。
#221 后经接管审查修复 WAV RIFF 尾部完整性，以最终 head `f326f5d` 的 14 项 CI 成功后合入 `41e29b6`。
下表的候选/验证/现场是原时点事实，当前组合和接收状态转到[项目经理基线](2026-10-26-project-manager-baseline.md)；不追溯改挂历史证据。
用户已要求结束旧排期，后续 owner、优先级和窗口由新负责人重新安排。
[原 Goal](2026-10-23-team-a-goal.md)和[原下一阶段计划](2026-10-26-team-a-review-and-next-phase.md)
已标记 superseded；历史 KR、F01–F12 和原完整范围继续保留，不据交接结算验收。

## 原交接时点的事实

| 对象 | 固定版本及状态 |
| --- | --- |
| 已集成基线 | main `54daef6ae4792b69a0e5e8645b09240ff715082e`，包含 B #218/#219；后续先重新 fetch/核对主干 |
| 本 PR 实现 | `b7e65f4a119999e720d594200c53dc683bec5526`，分支 `codex/team-a-final-handoff`；媒体提取源为 `230031043a76ac6c2d04ee6bb2fe93ca38f991c5` |
| 本 PR 后续文档 | 本版 1.1.0 补完整交接与 issue 索引，产品实现保持 b7e65f4；最终文档 head/检查以 PR 为准，旧实现检查不改挂到新 head |
| 既有实现 CI | [CI40300644125](https://github.com/hrygo/SpeechNexus/actions/runs/40300644125) 对 b7e65f4 全 14 作业成功，包括六个 PG/InnoDB/MinIO 分片、Python、Web/a11y、静态/契约及构建；临时检查进程/规则清理步骤成功 |
| 待审交接 | #221 尚未合并，B 的 [#220](https://github.com/hrygo/SpeechNexus/pull/220) 也尚未合并；本地候选、PR、main 和部署资格分别判断 |
| 完整验收 | 真实公开 ASR/TTS 链 **0/2**，A 整包 **0/8**，原工作包 **0/25**，Gate **0/6**；F01–F12 完整矩阵未验，真实链关键违规基线未知 |

B 在 #220 提供[项目整体交接](https://github.com/hrygo/SpeechNexus/blob/45bc90ed0fcc36264022b0c2f684e3d9006685ec/docs/audits/2026-10-26-project-handoff.md)
与[团队 B 交接](https://github.com/hrygo/SpeechNexus/blob/45bc90ed0fcc36264022b0c2f684e3d9006685ec/docs/audits/2026-10-26-team-b-handoff.md)，
这里固定引用其已推送 head `45bc90e`，不假定这些文件已进入本 checkout 或 main。
本文可独立接管 A 的范围；B 运行原件、C 本机制品仍须向原 owner 取得并校验。

## A00–A07 已交付、未完成与接收条件

| 工作包 | 已有交付与证据入口 | 尚缺的完整范围／接收条件 |
| --- | --- | --- |
| [A00 #19](https://github.com/hrygo/SpeechNexus/issues/19) | #141 `a7f4d46` JSON/runtime，#169 `7dc276a` 二进制/SSE SDK，#175 字面量门禁、#181 限定 RTE、#193/#201 固定词典 GET；本 PR 补私有检查同源类型 | 公开音频错误/投影、SSE 正式消费、完整跨字段/时序及 REST/WS/MCP 同版语义。C #177 已升下载 dev1，不能沿旧 dev0 约束消费 |
| [A01 #20](https://github.com/hrygo/SpeechNexus/issues/20) | #171 `d876585` 汇合 18 端口/7 查询/调用方与双库[证据地图](../audits/2026-10-25-coordination-port-evidence.md)，#173 `78505a8` 修复登录/有效身份 | 权威策略/readiness、生产 Scheduler/Worker、部分 B 查询 envelope/wrapper 对照及全范围联合证据；地图存在不等于端口已完成业务装配 |
| [A02 #21](https://github.com/hrygo/SpeechNexus/issues/21) | #139/#172 `dbbffe2` 配置原意图，#185 `7851cfb` 私有词典，#187 `5afb488` 资源身份守卫，#155 `f9dfa7c` 受限进程；本 PR 的媒体/回执见下节 | 公开音频 ready、可信 provider、发布/激活 CAS、最终参数、固定 Artifact/receipt/Operation，以及真实 HTTP/隔离 PG/MinIO 恢复 |
| [A03 #22](https://github.com/hrygo/SpeechNexus/issues/22) | owner/claim、完整配方准入、Job/outbox、父预算/leases 与 unknown 恢复组件；#171 汇合证据，B #218 有隔离 lease 结算 | 正式调度循环、公平排队、业务 quota/区间计量/结算和可信执行/停止来源；不能把六字段 JobFact 当完整公开 Job |
| [A04 #23](https://github.com/hrygo/SpeechNexus/issues/23) | Workspace/配置草稿/显式默认 HTTP；#201 `064c6b6` 固定 draft GET、最小 SELECT 与 404/503 区分 | 标准 ASR/TTS、完整 Job/Operation/事件/发布下载、配置创建/list/发布与完整管理/MCP；只读 draft 不等于发布 |
| [A05 #24](https://github.com/hrygo/SpeechNexus/issues/24) | 契约/解析基础，整包 planned | 公开实时会话/双工、owner/epoch、credit/gap/水位、冻结文本、停止及断线恢复，与 B04/C04 同版联验 |
| [A06 #25](https://github.com/hrygo/SpeechNexus/issues/25) | 候选/复用/发布边界已定义，整包 planned | 设计与克隆分别取证，材料固定、换文复用、显式 VoiceRevision 发布及退役/禁用/删除竞争；builtin TTS 不等待整包 |
| [A07 #26](https://github.com/hrygo/SpeechNexus/issues/26) | 文本删除账本、tombstone、maintenance 和 unknown 保护；与 C 独立副本边界已明确 | 音频/跨模块删除、生产迁移/最小角色、恢复先重放账本、备份/回退及冻结 RPO/RTO、目标拓扑、SDK/MCP 安装和许可运维交接 |

历史交付详情和失败按[复盘](2026-10-26-team-a-review-and-next-phase.md)及原 PR/CI 的受检版本保留。
新负责人接收的是现有成果与完整未完范围；本表不增加与原 AC 平行的验收体系。

## 本 PR 制品、消费者和兼容边界

| 制品 | 入口 | 当前事实与接收条件 |
| --- | --- | --- |
| 共享媒体包 | [packages/media](../../packages/media/README.md)、[源码映射](../../packages/media/source-inventory.json) | `speechnexus-media 0.1.0.dev0`；原类型逐字节迁移，decoder 仅换原生类型 import；gateway 原入口导出同一对象。A 自测通过，B 尚待消费固定源码或重建 wheel 后回执兼容范围 |
| 私有检查命令 | [媒体交接](2026-10-26-a02-media-kernel-handoff.md) | 有界哈希/完整解码，输出输入摘要/长度和源时钟；仅 PCM WAV/显式 raw PCM，WAV RIFF 长度/声明样本与实际解码一致；B 原 FLAC/MP3/Ogg decoder 行为保留，不自动纳入本检查 profile |
| 私有成功回执 | [canonical schema](../../contracts/internal/audio-inspection.schema.json) | `audio_inspection` 1.0.0；Python/TS 同源生成，失败/取消/未知无成功事实，不含正文/路径或发布、停止、预算许可 |
| 窄消费者 | [ProcessAudioAssetInspector](../../backend/src/speechnexus_backend/speech/infrastructure/audio_asset_inspector.py)、[回执交接](2026-10-26-a02-audio-receipt-handoff.md) | 仅 asr_input PCM WAV；绑定实际摘要/长度、整数/源时钟/时长上限及末次取消。text_input 沿用既有行为，voice_reference 不可用；无效回执保持 unavailable |
| 组合根与构建 | [检查边界](../architecture/audio-asset-inspection.md)、Python Dockerfile/uv workspace | 默认 AssetRuntime **仍为 TextAssetInspector**。Dockerfile 增加共享包 COPY，但未实际构建/安装批准检查镜像；无新增公开 HTTP、迁移/grant、Job/预算或发布写入 |

B [#219](https://github.com/hrygo/SpeechNexus/pull/219) 于 2026-10-26 16:16:13（Asia/Shanghai）合入 `54daef6`，
[精确合后 CI](https://github.com/hrygo/SpeechNexus/actions/runs/40291095616) 14 项成功。
[固定交接](../architecture/file-tts-binding-handoff.md)提供模型 revision、11 个运行制品、镜像/源码与完整注册输入；
A 未重跑其 4.52 GB 原件或 GPU 实验。注册输入不创建权威 binding、有效 manifest、资格、已发布声音或默认指针。

## 跨队接收件与待决事项

| 接力／决定 | 原职责边界；新 owner 待指定 | 解除条件 |
| --- | --- | --- |
| A → B 共享媒体 | A 打包/接线，B 保留 codec 语义和资格 | B 接收本 PR 固定源码/重建 wheel，核对旧入口类型/异常身份、格式与完整性边界；A 的 1225 网关回归不代签 B |
| B → A 执行与权威配置 | B 固定模型/镜像/命令、执行/停止事实；A 注册/资格校验/发布 | 接收 #162/#219 输入后核实权威 binding/defaults、适用 builtin VoiceRevision 和有效容量引用；固定部署/argv/端点与执行身份，不用草稿或 fixture 补值 |
| A → C 公开链 | A 发布完整 API/有效配置；C 只经公开协议消费 | 同版音频 ready、创建/查询/取消 Job、Artifact/receipt/下载及错误样例；由标准 SDK/页面验证实际结果，健康检查不代替业务调用 |
| 固定 binding 与未知提交 | A/C 冻结公开语义 | 不自加 binding_revision_id；明确固定资源表达/受理前置条件，完整解析并校验 Job/workspace/kind/pin/结果关联。外发前持久保存完整意图，HTTP 在事务外；无键不承诺恢复，不隐含按键查询。删除/撤销后的未知重放仍待定 |
| CustomVoice 参数 | A/B 固定有效上限，C 测边界 | 一次 intent ≤400 codepoints、pronunciation=null；Nexus instructions 2000、标准/内部命令 4096、引擎默认 500 分列。模板目标 2000 不等于实际接收；冻结 argv 及 2001–4096 的准入/拒绝，不静默截断 |
| 隔离数据层和迁移 | A 统一迁移框架/平台最小角色，C 自有表片段和对象区，B 执行隔离资源 | 明确配置键、资源归属、访问边界/清理身份和真实窗口；凭据经批准配置链交付，本文不写值。C 不共写底座表，不另建预算权威；fresh schema 不替代升级迁移 |
| C 自有恢复/结果消费 | C 实施；A 评审公开幂等，B 核对执行事实 | #42 先外发后落库、同键异体/CAS/source、请求键跨刷新、错误分类、扫描饥饿/退避、SQL 读取和异步隔离审查未结；修复后以真实持久化反例留证。C-N02 不阻塞独立 C02 |

三队按制品分段接力，不要求长工作包全部关闭。ASR profile 可选，builtin CustomVoice 不等待完整设计/克隆资格。
公开 Job 无 status=unknown；应用未决、HTTP 取消、执行终态、单请求释放、整代停止、lease 和 base 分别留证。
ASR 旧隔离实验包含 base 结算；#218 的 TTS 实验未结 base，不可合称全部资源释放。
C 本机 v1.1.0 计划的 SHA256 `66fccff9c80f0c8c25395a111a19b97cdce3ef13ac620fede3acaaa13b236205`
仅按采纳回执记录，A 未取得并独立复核完整文件，不假定其已在 main。

## 全部 issue 与验收现状

[40 项交接清单](../audits/2026-10-26-team-a-issue-handoff.json)包含逐项编号、标题、状态、更新时间和公开链接。
清单核验时间为 **2026-10-26 18:13:27 +0800（Asia/Shanghai）**，在他人本轮写入结束后再次读取 GitHub；
40 项均 open，34 项部分实施、6 项 planned。
每项只有一个有效 status 标签。依据用户要求，现有正文已整合替换而非追加更新段，合并重复任务和历史摘要，
总量 93001 → 72246 字符；目标与整个 AC 章节逐字保留，标题/负责人/里程碑/状态标签不变。
[核对回执](https://github.com/hrygo/SpeechNexus/issues/6#issuecomment-6412962541)及稳定根的前后快照提供审计索引。

| Issue | 当前状态 | 交接范围 |
| --- | --- | --- |
| [#6 总控](https://github.com/hrygo/SpeechNexus/issues/6) | open / 部分实施 | SpeechNexus 三团队整体实施与验收跟踪 |
| [#7 Epic S0](https://github.com/hrygo/SpeechNexus/issues/7) | open / 部分实施 | 接口、环境与关键风险探针 |
| [#8 Epic S1](https://github.com/hrygo/SpeechNexus/issues/8) | open / 部分实施 | 标准文件语音与基础页面闭环 |
| [#9 Epic S2](https://github.com/hrygo/SpeechNexus/issues/9) | open / 部分实施 | 实时语音、音色与实例生命周期 |
| [#10 Epic S3](https://github.com/hrygo/SpeechNexus/issues/10) | open / 部分实施 | 完整语音底座与基础产品试点 |
| [#11 Epic S4](https://github.com/hrygo/SpeechNexus/issues/11) | open / 部分实施 | 会议、提词、助手与有声书 |
| [#12 Epic S5](https://github.com/hrygo/SpeechNexus/issues/12) | open / 部分实施 | 目标拓扑、恢复与完整交付资格 |
| [#13 Gate G0](https://github.com/hrygo/SpeechNexus/issues/13) | open / 部分实施 | 接口、环境与关键风险探针验收 |
| [#14 Gate G1](https://github.com/hrygo/SpeechNexus/issues/14) | open / 部分实施 | 标准文件语音与基础页面闭环验收 |
| [#15 Gate G2](https://github.com/hrygo/SpeechNexus/issues/15) | open / 部分实施 | 实时语音、音色与实例生命周期验收 |
| [#16 Gate G3](https://github.com/hrygo/SpeechNexus/issues/16) | open / 部分实施 | 完整语音底座与基础产品试点验收 |
| [#17 Gate G4](https://github.com/hrygo/SpeechNexus/issues/17) | open / 部分实施 | 会议、提词、助手与有声书验收 |
| [#18 Gate G5](https://github.com/hrygo/SpeechNexus/issues/18) | open / 待实施 | 目标拓扑、恢复与完整交付资格验收 |
| [#19 A00](https://github.com/hrygo/SpeechNexus/issues/19) | open / 部分实施 | 契约类型生成、语义样例与接口交接 |
| [#20 A01](https://github.com/hrygo/SpeechNexus/issues/20) | open / 部分实施 | 生产协调事务、迁移权限与 18 端口验收 |
| [#21 A02](https://github.com/hrygo/SpeechNexus/issues/21) | open / 部分实施 | 资产、不可变版本与对象原子发布 |
| [#22 A03](https://github.com/hrygo/SpeechNexus/issues/22) | open / 部分实施 | 调度准入、持久任务、配额与未知恢复 |
| [#23 A04](https://github.com/hrygo/SpeechNexus/issues/23) | open / 部分实施 | 标准文件语音、任务与管理 REST/SSE |
| [#24 A05](https://github.com/hrygo/SpeechNexus/issues/24) | open / 待实施 | 公开实时协议、双工准入与流恢复 |
| [#25 A06](https://github.com/hrygo/SpeechNexus/issues/25) | open / 待实施 | 音色候选、复用校验与版本发布 |
| [#26 A07](https://github.com/hrygo/SpeechNexus/issues/26) | open / 部分实施 | 删除账本、备份恢复与平台发布交付 |
| [#27 B00](https://github.com/hrygo/SpeechNexus/issues/27) | open / 部分实施 | MegaCube 环境、模型可行性与停止证据探针 |
| [#28 B01](https://github.com/hrygo/SpeechNexus/issues/28) | open / 部分实施 | 执行网关、持久意图与真实提交屏障 |
| [#29 B02](https://github.com/hrygo/SpeechNexus/issues/29) | open / 部分实施 | 受限节点控制器与实例生命周期 |
| [#30 B03](https://github.com/hrygo/SpeechNexus/issues/30) | open / 部分实施 | 真实文件 ASR/TTS 与有界媒体流水线 |
| [#31 B04](https://github.com/hrygo/SpeechNexus/issues/31) | open / 部分实施 | 流式 ASR/TTS、PCM 背压与输出区间账本 |
| [#32 B05](https://github.com/hrygo/SpeechNexus/issues/32) | open / 待实施 | VoiceDesign/Base、克隆与声音材料恢复 |
| [#33 B06](https://github.com/hrygo/SpeechNexus/issues/33) | open / 部分实施 | VAD、对齐与匿名分人能力 |
| [#34 B07](https://github.com/hrygo/SpeechNexus/issues/34) | open / 待实施 | 模型部署、混合负载与目标拓扑资格 |
| [#35 C00](https://github.com/hrygo/SpeechNexus/issues/35) | open / 部分实施 | 工作台版本骨架、契约客户端与开发 fake |
| [#36 C01](https://github.com/hrygo/SpeechNexus/issues/36) | open / 部分实施 | AdminUI 与持久任务中心 |
| [#37 C02](https://github.com/hrygo/SpeechNexus/issues/37) | open / 部分实施 | 转写校对导出与文本配音闭环 |
| [#38 C03](https://github.com/hrygo/SpeechNexus/issues/38) | open / 部分实施 | 音色创作、校验发布与复用工作室 |
| [#39 C04](https://github.com/hrygo/SpeechNexus/issues/39) | open / 部分实施 | 浏览器采集播放、实时字幕与媒体生命周期 |
| [#40 C05](https://github.com/hrygo/SpeechNexus/issues/40) | open / 部分实施 | 会议封存、校对与提词跟随 |
| [#41 C06](https://github.com/hrygo/SpeechNexus/issues/41) | open / 部分实施 | 内网 LLM、会议纪要与语音助手 |
| [#42 C07](https://github.com/hrygo/SpeechNexus/issues/42) | open / 部分实施 | 有声书局部重做、保存副本与独立导出 |
| [#43 C08](https://github.com/hrygo/SpeechNexus/issues/43) | open / 部分实施 | 浏览器、业务删除恢复与应用交付资格 |
| [#142 B-G1](https://github.com/hrygo/SpeechNexus/issues/142) | open / 部分实施 | 真实文件 ASR/TTS 持久执行与制品交接 |
| [#149 A→C06](https://github.com/hrygo/SpeechNexus/issues/149) | open / 待实施 | 提供真实 Speech Job 与已发布音色，解除助手成功路径阻塞 |

该表是交接时点快照，不是新的派工或完成率。后续先读当前 issue 再修改，保留并行更新；
仅有部分实现仍为 in-progress，无实际开工的 #18/#24/#25/#32/#34/#149 保持 planned。
证据、任务交付、AC、GitHub state 和 Gate 放行分别判断，不因 PR/CI 或交接完成而批量关闭。

## 验证、失败与原件

| 范围 | 实际结果与版本边界 |
| --- | --- |
| 实现提交 b7e65f4 | CI40300644125 全14成功；完整 Python、六个 PG/InnoDB/MinIO 分片、Web/a11y、契约/静态、部署配置/构建与检查规则清理通过；不代表公开链/目标拓扑资格 |
| 本地 Python | 基于 main54daef6 + 本实现，契约/资产/窄消费者/实际受限媒体进程/gateway 共4395通过、无跳过（3170 + 1225） |
| 本地 TypeScript | 契约类型检查通过，1987测试通过、无跳过 |
| 静态/契约 | Ruff1131文件格式/检查、mypy854文件、8依赖边界、33份源契约、生成漂移和相对54daef6的六项兼容/变化检查通过 |
| 构建/树外消费 | 五包wheel/sdist；从wheel实际解码4800样本，合法回执通过/错摘要拒绝；禁止gateway/controller导入并核对均从wheel加载。Compose config和此前Docker COPY最小上下文frozen安装通过，未实际构建部署镜像 |
| 本版交接文档 | 只改文档与审计索引；运行链接/frontmatter/JSON/清单覆盖及命令语法、diff检查。新增文档head的CI以PR为准，不借旧head结果声称通过 |

命令、日志 SHA 和 wheel 摘要见[机器证据索引](../audits/2026-10-26-team-a-closeout-evidence.json)。
初次统一契约缺样例/根引用、解析期间取消 RED、缺 Node 依赖均修复并回归；原失败保留。
旧 #185 ENOSPC/覆盖失败、#173 合后 Web 失败、#155 中间取消/清理未知及 #201 精确合后取消/清理未知
仍按各 issue/PR 的原版本独立记录，新结果不改写它们。
本地未执行数据库/MinIO 联验、真实模型/公开 SDK 页面或物理拓扑；本 PR 无新 GPU/运行态实验。
C 82份原件丢失影响278子条件中的214个引用，全部278仍not_run；不能用历史勾选补证，重建见 #35。

原件稳定根为 `/workspace/.speech-nexus-team-a-evidence/2026-10-26-final-handoff/`，`manifest.json` 逐文件校验：

| 原件 | 作用／恢复方式 |
| --- | --- |
| `final.patch`、manifest中的commit/base | 完整实现源码补丁；`staged.patch`仅为更早暂存快照，不用它单独恢复最终实现 |
| `final-checks/` | 最终本地日志、五包dist、`verify_wheels.py`及`wheels.json`；在树外复核制品，匹配摘要后使用 |
| `prior-receipt-checks/`、`prior-media-build/` | 旧回归/构建和RED/中间记录，保持历史版本；不把旧wheel当最终制品 |
| `ci-jobs.json` | b7e65f4精确14作业及清理步骤回读 |
| `issues-before-organic.json`、`issues-after-organic.json`、`issues-organic-audit.json` | 整合前后完整正文、状态及逐项回读；`implementation-tracking-final.md`是原38项编排视图，另2项见40项清单 |

该稳定根属于 A 执行环境，不能作为 B/C 或新机器的下载地址。需要原件时先确认该环境仍存续，按 manifest
校验后经批准渠道交付；若不可取得，保留“证据未取得”并按 PR 源和固定依赖重建，不伪造恢复。
公开可获取入口是 PR 源码、CI 和仓库证据摘要，私有原件/凭据不上传。

## 工作区、资源与恢复边界

当前 A 工作区为 `/workspace/SpeechNexus`，本轮检查到一个 worktree；实现已提交并推送，后续只提交本版交接文档。
没有启动本地数据库/MinIO/模型，也未操作 B GPU、35B、embedding 或 reranker；不据此推断其他环境已停止或完成清理。
此前 A 环境44树/53分支可恢复清理、旧5树/4分支/3服务和容量记录只属于当时环境，见 #6，
本轮不重新执行其清理，也不删除未知work、日志、模型、数据库、对象或失败证据。

批准镜像、公开端点、权威资源/资格、隔离服务凭据和实验窗口仍是具名缺项。
凭据只记录配置键/交付链，不放入源码、PR或日志；B原件/语料/GPU保护按其[交接](https://github.com/hrygo/SpeechNexus/blob/45bc90ed0fcc36264022b0c2f684e3d9006685ec/docs/audits/2026-10-26-team-b-handoff.md)由原owner交回执。
内容留在部署网络，LLM默认关闭且无公有云回退；许可、质量和物理拓扑不能由文件数量或CI替代。

## 新负责人接管步骤

1. **固定版本和写入归属。** 先读 #6、#220、#221 及本文件，再fetch并记录main/head/契约版本；检查现有工作树，候选在独立checkout评审，不覆盖未知改动。#220和#221分别审阅，未合并不称main已交付；本交接不自动授权合并、部署或新实验。
2. **接收实现和原件。** 获取本PR固定源码、源码映射及可重建wheel；由B回执兼容范围。核对A稳定根manifest、CI和issue快照；B/C原件各由其owner交付，取不到的证据明确列缺。
3. **接收未完范围和决定。** 从A00–A07表及全部40项issue逐项确认剩余实现、接线、资格与真正阻塞；指定集成维护者和迁移/角色/资源owner，冻结binding、未知提交、指令映射及输出上界，不降低原AC。
4. **重新安排实施与验收。** 新负责人基于可获取制品、环境和实际容量决定顺序/窗口；原ASR→CustomVoice计划只保留历史。可以分段接入与继续独立任务，不把整包/Gate或C07恢复变成所有工作的串行屏障。
5. **完成同版交接回执。** 每项分别记录“已接收／候选待集成／尚缺”、SHA/原件摘要、行为结果和限制；缺停止/释放仍占用，原件重建先于验收补勾。三队各确认自身专业范围，Gate由用户放行。

以下为重建实现组件的入口，需使用仓库Node26/pnpm12和uv锁定环境；不加载模型或自动部署：

```sh
uv sync --locked --all-packages
pnpm install --frozen-lockfile
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

复验成功只覆盖命令实际范围。交接完整不表示工作完成，旧排期不自动续作，后续安排由新负责人决定。
