---
title: "SpeechNexus 团队 B 执行目标"
status: active
version: "0.5.0"
date: 2026-10-24
---

# SpeechNexus 团队 B 执行目标

## 目标

作为 MegaCube GPU 服务器上的团队 B，负责语音与执行：执行网关、持久提交屏障、引擎 adapter、媒体流水线、受限节点控制器、执行与停止事实、模型与容量资格。按照[三团队实施方案](2026-10-23-three-team-delivery-plan.md)和[总控 #6](https://github.com/hrygo/SpeechNexus/issues/6)，交付 B00–B07（[#27](https://github.com/hrygo/SpeechNexus/issues/27)–[#34](https://github.com/hrygo/SpeechNexus/issues/34)）；向 A 交付可服务能力、manifest/资格、实际执行关联、输出水位与可信停止事实，向 C 交付经公开协议可解释的能力/就绪原因。目标从规划 PR 合并后的 `main` 启动，不把已定义的契约、fake 原型或"模型能启动"当作执行链路与模型资格已实现。

B 独占网关侧事务端口 `seal_barrier`、`create_submit`、`mark_execution_unknown`、`record_stop`、`record_execution_observation`、`redact_execution_command`、`append_interval`，以及 `query_outcome` 中自身事实的查询实现；A 维护总矩阵与验收。执行控制/执行流契约、引擎事实、停止证据和目标拓扑/模型资格由 B 唯一维护，A 参与评审，涉及浏览器行为时加 C。

本文件是可执行目标定义，不证明应用原生 Goal 已启用。原生 Goal 的启用状态以 Codex 应用或目标工具返回结果为准。

## 当前对齐（2026-10-24）

以 main `a180469` 为本轮开工基线；[PR #121](https://github.com/hrygo/SpeechNexus/pull/121)
已封闭外部快照写权威事实旁路，节点快照仅作诊断。
原生 Goal 保持 active；以下只记录分段交付，不关闭父包或 Gate。Issue 顶部当前状态、任务勾选和
唯一状态标签已统一，分轮实测保留在折叠历史中。

| 工作包 | 状态 | 已交付与剩余 |
| --- | --- | --- |
| B00 #27 | in-progress | 环境、ASR/TTS/VAD 分段探针已有；完整许可、请求级停止与拓扑资格待补 |
| B01 #28 | in-progress | 7 个事务、HTTP/drain、有界恢复及可信节点停止来源已有；真实引擎、默认周期驱动与完整闭环待补 |
| B02 #29 | in-progress | 本地观察/检查点恢复、真实 ASR 预热及可信停止查询已有；真实引擎故障、ready监控与 A 完整闭环待补 |
| B03 #30 | in-progress | 有界媒体、文件 ASR、CustomVoice TTS 已有；公开入口、持久执行、对象发布及 Base 待补 |
| B04 #31 | in-progress | 区间账本已有；真实双工、PCM 背压、完整配方与计量待补 |
| B05 #32 | planned | VoiceDesign/Base、克隆、声音材料恢复和发布完整路径未开工 |
| B06 #33 | in-progress | CPU VAD 与文件消费者已有；流式接入、词级对齐、匿名分人和质量资格待补 |
| B07 #34 | planned | 已收集资格输入；模板、混合负载、长时与物理多主机资格未开工 |

本轮把[节点可信停止查询接入网关](../architecture/trusted-node-stop.md)：只有原 stop
回执与本地 released 检查点完全匹配才出具事实，旧成功回执不追溯认证；网关经固定 TLS
验证原绑定、代次、任期及时间，沿用原停止事务关闭每条执行，不写 A 预算。
原生 TLS/SQLite/双库消费者验证半证据保留占用、重启补齐、共享实例多执行和 A 幂等归还。
默认网关周期驱动、真实引擎故障、ready 实例持续监控、完整执行/预算与物理多主机资格仍待。
真实引擎适配由 B 主责，A 配合协议消费者与预算/发布边界，不能把 B 自有适配缺口一概标为
等待 A 提供引擎协议。实施选择由团队处理，不要求用户判断这些技术细节。

## 首个里程碑：B00 与 B01/B02 的最小准备

- 先做只读前检：OS/架构/CPU/GPU/内存/驱动/CUDA、容器与磁盘、已安装制品、现有进程/端口和资源占用。不从机器名称或设备型号推断资格，不改变既有服务、凭据或驱动。
- 完成 [B00 #27](https://github.com/hrygo/SpeechNexus/issues/27)：交付真实环境与能力缺口矩阵，覆盖文件 ASR、TTS、实时、VoiceDesign/Base 的候选版本、输入输出、request ID、status/abort、强输出上界、停止/资源事实与加载峰值；给出 manifest 与停止证据草案。探针只在本轮明确获准的环境、制品和合成或授权语料上运行，记录模型/镜像摘要、可重复步骤与清理结果；没有许可或制品时报告缺口并继续独立 fake，不自动下载模型、改驱动、停服务或扩大暴露端口。
- 在 [B01 #28](https://github.com/hrygo/SpeechNexus/issues/28) 实现第一个闭合单元，例如"持久唯一 intent + 同键重放不再次 dispatch"，并给出可被 A 消费的 fake engine/controller 故障场景：同意图不重复外发、提交确认丢失查原身份、未外发失败不伪造 stop。
- 在 [B02 #29](https://github.com/hrygo/SpeechNexus/issues/29) 交付受限 node-controller 的 prepare/stop、generation、peak→steady、drain/停止/释放 fake 与接口草图：只作用于批准模板与固定节点，迟到旧 generation 不伤新实例。
- 与 A01/A03 的 runtime 端口对接，不另建一套预算权威；只在 `execution-gateway/` 与自有 fake/adapter/测试范围写入。controller 新包、迁移、`deploy/` 与公共锁文件变化先交 A 排队集成。
- 向 A/C 交付：环境事实与探针实测（含未验证项）、模型/镜像/许可清单与 manifest 草案、fake 故障接口包、网关首个单元与测试、GPU 当前状态与清理结果、下一张 B01/B02/B03 任务卡。

首轮验收：A 能消费停止、unknown 与迟到结果场景；真实探针带模型/镜像/硬件版本和可重复步骤；缺失 stop、输出上界或资格的路径明确受限；现有 GPU 服务与凭据未被破坏。

## 后续路线与边界

按具名制品依赖推进 B01 → B02/B03 → B04/B05/B06，B07 从 B00 起建模板并随每个实际能力持续补证。B06 的 VAD 先于 B04 交付；实时配方中的 VAD、1.7B 完整段复核和 required 辅助能力必须全部可用后才进入真实准入。B04/B05 与 A05/A06、C03/C04 联调；B07 最终与 A07/C08 合并验收。

- B 是唯一 GPU 运行态维护者：GPU 实验排队执行，探针前核对现有进程/端口/资源；其他团队不在同一 MegaCube 各自加载模型或并发跑负载。
- 取消已接收、HTTP 断开、TTL 到期和连接结束都不是停止证据。只有拿到停止与资源释放的证据后才归还预算；结果未知时继续隔离并占用相关预算，不用 fake 冒充真实 stop。
- 输出流区间账本归 B，A 消费用量事实并维护业务配额与结算；不各写一份 PCM 区间权威，不替 Scheduler 释放预算。
- 只有一个 GPU 主机时，多进程只标逻辑集群；单节点丢失可验证安全隔离与恢复，不能宣称多算力主机接管或 GPU 横向扩容资格。
- 引擎凭据、提交屏障、fencing、abort 与完成证据留在执行网关边界；业务 API、worker 和工作台不获得直接引擎访问或任意容器管理能力，不挂载 Docker socket。
- 内容边界与 A/C 一致：模型权重、私有音频、完整转写、声音特征和内部模型路径不进入日志、遥测或 Git；外部制品只传摘要与部署内引用。
- 契约遵循四件套（schema/类型、正负与时序样例、fake、升级影响），公共生成器与共享 schema 由 A 独占维护，B 不手改生成产物；上游包按已发布的固定版本消费。
- 每个任务在获得当前会话 Git 授权时使用 `task/b-<id>-<slug>` 短分支与 PR 交付，保护分支检查通过后才合并。模型下载、驱动/容器变更、端口暴露和部署等运行态操作按具体任务授权执行，不因目标持续执行而自动获得。

## 完成与阻塞条件

团队 B 的范围只有在 B00–B07 的具名产物、真实运行证据与资格报告齐全，且 A/C 能以原身份和版本核验后才算完成。G0–G5 由三队联合取证、总指挥决定放行；B 端交付完成不等于其他团队或全项目完成，不能越权关闭 Gate，也不能把"待资格验证"对外宣称 ready。

缺少 A 的接口包、迁移顺序或预算权威，缺少制品许可、环境窗口或批准入口时，继续不依赖该输入的 fake、契约与独立任务；依赖工作明确标为待联调或阻塞，报告所缺制品、owner 和解除条件。不能用 fake、schema 通过、健康检查、模型能启动或单个 happy path 替代真实闭环与资格验收，也不在无可继续任务时无限重复检查。

每轮开工前先更新相关 Issue 的唯一状态标签、正文任务状态和本轮验收单元；交付后同步勾选
已完成子项、证据与未完成范围，刷新矩阵并提供下一轮建议。历史状态滞后时按已合并代码和
实测记录更正正文，不只追加评论；不因 PR 合并自动关闭整个父工作包。

## 可用于原生 Goal 的目标文本

```text
作为 SpeechNexus 团队 B，依据 main 中 docs/plans/2026-10-23-team-b-goal.md、三团队实施方案和总控 issue #6，持续完成 B00–B07 的执行网关、引擎与受限控制器交付。先从 B00 #27 的 MegaCube 只读前检环境与能力缺口矩阵开始，同时交付 B01 #28 的持久 intent/提交屏障首个单元和 B02 #29 的 controller fake，再推进真实文件 ASR/TTS、流式与音色、VAD/对齐/分人，最终到 B07 的目标拓扑与混合负载资格。遵守 B 独占 GPU 运行态与输出区间账本、只有停止与资源释放双证据才退预算、未知继续占用、不另建预算权威、单机多进程只标逻辑集群、内容不出网与凭证边界，并按当前授权通过短分支和 PR 交付。每轮更新 Issue/证据/矩阵。不能替代 A 的平台事务或 C 的产品工作；缺制品、许可或环境时继续 fake 与独立任务并明确阻塞。只有团队 B 全范围有真实证据时完成目标，不能仅因 fake、schema、模型能启动或代码合并而宣称执行与模型资格通过。
```

[三队启动指令](2026-10-23-agent-team-startup-briefs.md) · [跟踪矩阵](2026-10-23-implementation-tracking.md)
