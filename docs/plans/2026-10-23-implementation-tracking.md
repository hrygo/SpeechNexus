<!-- speechnexus-implementation:PROGRAM:v1 -->
# SpeechNexus 三团队整体实施跟踪

总控：[PROGRAM #6](https://github.com/hrygo/SpeechNexus/issues/6)。本次状态复核：2026-10-25 08:39 UTC（北京时间 16:39）。
冻结主干：33233c8a42b284b627e891b32b1b28faeafb30e7；issue正文、唯一状态标签和实际AC证据为事实来源。
原规划38条；新增#142阶段任务、#149交接项单列，合计40条非PR issue，不改变25主包/6 Gate分母。

**工作包完整验收：0/25；Gate放行：0/6。** 21个主包in-progress，4个planned（A05/A06/B05/B07）。
步骤完成、组件合入、本机未提交实现与业务/拓扑资格分开；不把PR/测试/文件数量换算产品完成率。

## 指挥与环境

| 团队 | 环境 | 主责 |
| --- | --- | --- |
| A | 云开发环境 | 平台/协调/迁移/公开API；共享契约、生成与串行集成 |
| B | MegaCube GPU | 网关/控制器/模型/真实资格；GPU排队与隔离Linux拓扑 |
| C | MacBook | workbench后端/Web/浏览器媒体/高级业务与体验 |

用户统一派工与Gate放行。按同一integration_base_sha交付制品、契约/生成器及具名证据，不共享本地业务目录。
PG为正式数据库，InnoDB为共享协调对照；停止与释放双证据后才由A原事务退预算一次。

## 当前派工入口

**先闭合一条可恢复的真实文件 ASR 公开链路，再沿同一协议接 CustomVoice TTS。**
本次回读全部 40 条非 PR issue、5 个开放 PR、精确 head 检查和当前本地候选；
40 条均 open 且各有唯一 status。主干[CI40168289503](https://github.com/hrygo/SpeechNexus/actions/runs/40168289503)
14 项成功，当前尚无公开语音业务/物理集群联合验收。
历史组件说明见[原复盘](2026-10-25-team-a-realignment.md)，最新 B 事实和终审缺口见
[团队 B 目标](2026-10-23-team-b-goal.md)；历史测试不改挂到本轮草稿。

| 团队 | 当前可证事实 | 下一交接与真实缺口 |
| --- | --- | --- |
| A | 协调原事务/未知恢复、文本 Asset/Workspace、草稿及默认快照 HTTP 已合入；#139 只交付 P1a 原意图/只读边界。#155 head `0db7a4c` 提交独立音频检查进程候选，本轮回读 CI40177149300 已失败 | A 修复当前 CI 后仍需共享媒体/镜像与真实音频 ready；接权威配置、完整业务 Job/准入、Artifact/Operation/receipt 和公开发布下载。监督器存在不等于媒体已检验 |
| B | #140/#147 已合入；#152 回执登记与 #154 异步输入已依次集成，PR/合后 CI 均成功；#158 交付提交后文件执行组件与双消费者/确认丢失回归 | #158 的最新组合与集成事实以该 PR 为准；继续接固定真实 ASR、可信停止/释放及 A/C 同版公开链，再接 CustomVoice；替身不构成模型资格 |
| C | 已提交 [PR #153](https://github.com/hrygo/SpeechNexus/pull/153)，head `6d3d092`，可获取候选；已有作者本机 PG/浏览器/LLM 等分层记录 | 与 main 有冲突，该 head 没有 checks/status/PR workflow；同步并保留最新 A/B 增量、取得新 head CI、迁移/组合根与 method+path 消费者核验，再接真实语音；不再称“未提交” |

#152 的 `0012_execution_results` 与列权限已按用户授权完成 A 范围串行接收，
合入 `ef16d71`；#154 随后合入 `0832074`，各自精确 PR 与合后 CI 均成功。
#158 的提交后文件执行组件覆盖独立消费者对同一原 intent 的实际入口竞争、
真实接收/结果 commit 确认丢失及线程创建失败清理；生命周期、双库与对象回归的
精确版本、CI 和报告见 [PR #158](https://github.com/hrygo/SpeechNexus/pull/158)。
识别/VAD 仍为替身，默认固定真实引擎、可信停止/释放、A 发布与公开链路资格仍待。
独立 worktree 的项目 skill 候选不抵扣业务 AC。
B 本轮继续补[受控 ASR 原生鉴权与代次检查](../architecture/controlled-asr-generation.md)：
复用 #161 预热修复，派生原生 key、检查实际容器配置并在文件命令读取音频前核对目标；
修正 ASR 样例及 file-start 的 generation/barrier 目标，五组双库/MinIO 消费者回归
102 项通过、无跳过。无 GPU 原生中间件验证不等于真实默认工厂、停止/预算或公开链已完成。精确候选和集成状态
以该单元 PR 和 #29/#142 为准，原 AC 继续保留。
B00/B01/B02/B03/B04/B06 保持 in-progress，B05/B07 保持 planned，不把待集成子项扩为整包 blocked。

A00 的 [#141](https://github.com/hrygo/SpeechNexus/pull/141) 已合入主干 `a7f4d46`，
补齐 `generateRenderUnit` 两个 runtime DTO 和 JSON HTTP 入口，沿用既有 Workbench 源契约。
另三个 downloadSavedAsset/downloadBookExport binary、streamExportEvents SSE 入口、
实际 C 路由、公开 SpeechJob 和音色资格仍需各自证据；旧 CI 只适用于原 base/head 组合。

文件 ASR 路线：真实 ready 音频 + 权威 binding/显式默认 → 完整持久 Job/原子准入 →
原 intent 唯一真实执行 → B 私有结果回执/可信停止 → A 发布/下载 → SDK/C 消费。
无 Profile 合法，但草稿/示例/引擎隐式默认不能充当权威配置。A 原事务唯一退预算，
取消/EOF/请求消失不作停止证明；unknown 保留占用。不新增预算或结果权威、不采用 etcd，Redis 非权威。

| 具名阶段/交接 | 归属与当前状态 | 解除条件 |
| --- | --- | --- |
| #142 真实文件 ASR/TTS | B03 #30 的阶段子任务，in-progress；关联 A #21/#22/#23 与 C #37 | 固定模型/镜像/许可和配置、真实持久执行/唯一回执、可信停止/预算、A 发布、SDK/C 同版正常及故障证据；先 ASR 后 CustomVoice |
| #149 助手上游输入 | A 提供/C06 接收，planned；关联 A04/A06/B05 | 可查询真实 SpeechJob + 已发布声音 + 取消/unknown/停止对账；默认关闭和合成 ID 不算成功，不前移为 G1 前置 |

先集成可审计组件并继续独立的配置/runner 工作，避免共同版本债务扩大；
不等待所有底层探针穷尽再装配业务，也不跳过真实故障验证。
实时、VoiceDesign/Base/克隆、对齐/分人、删除/恢复、MCP/完整 SDK、混合负载/长时和物理集群仍在原范围。
只有一个 GPU 主机不降低集群要求，逻辑竞争与物理多主机分别出证。

CI 继续以五分钟为优化目标；#154 为 242 秒、#152 为 334 秒（含排队），
前者本次达标，后者未达。保留必需检查和完整适用集合，历史失败/跳过据实记录；
[CI 范围](../architecture/ci-validation.md)及轻量路径不能用来证明新 Python/迁移候选已全量验证。
本次只核对状态、交接记录和文档，没有重新运行模型、业务质量或目标拓扑验收。

## 六阶段 Epic 与独立关口

| 阶段 Epic | Gate/状态 | 唯一主归属工作包 |
| --- | --- | --- |
| [S0 #7](https://github.com/hrygo/SpeechNexus/issues/7) [Epic S0] 接口、环境与关键风险探针 | [G0 #13](https://github.com/hrygo/SpeechNexus/issues/13) 实施中 | [A00 #19](https://github.com/hrygo/SpeechNexus/issues/19)、[B00 #27](https://github.com/hrygo/SpeechNexus/issues/27)、[C00 #35](https://github.com/hrygo/SpeechNexus/issues/35) |
| [S1 #8](https://github.com/hrygo/SpeechNexus/issues/8) [Epic S1] 标准文件语音与基础页面闭环 | [G1 #14](https://github.com/hrygo/SpeechNexus/issues/14) 实施中 | [A02 #21](https://github.com/hrygo/SpeechNexus/issues/21)、[A03 #22](https://github.com/hrygo/SpeechNexus/issues/22)、[A04 #23](https://github.com/hrygo/SpeechNexus/issues/23)、[B01 #28](https://github.com/hrygo/SpeechNexus/issues/28)、[B03 #30](https://github.com/hrygo/SpeechNexus/issues/30)、[C02 #37](https://github.com/hrygo/SpeechNexus/issues/37) |
| [S2 #9](https://github.com/hrygo/SpeechNexus/issues/9) [Epic S2] 实时语音、音色与实例生命周期 | [G2 #15](https://github.com/hrygo/SpeechNexus/issues/15) 实施中 | [A05 #24](https://github.com/hrygo/SpeechNexus/issues/24)、[A06 #25](https://github.com/hrygo/SpeechNexus/issues/25)、[B02 #29](https://github.com/hrygo/SpeechNexus/issues/29)、[B04 #31](https://github.com/hrygo/SpeechNexus/issues/31)、[B05 #32](https://github.com/hrygo/SpeechNexus/issues/32)、[C03 #38](https://github.com/hrygo/SpeechNexus/issues/38)、[C04 #39](https://github.com/hrygo/SpeechNexus/issues/39) |
| [S3 #10](https://github.com/hrygo/SpeechNexus/issues/10) [Epic S3] 完整语音底座与基础产品试点 | [G3 #16](https://github.com/hrygo/SpeechNexus/issues/16) 实施中 | [A01 #20](https://github.com/hrygo/SpeechNexus/issues/20)、[B06 #33](https://github.com/hrygo/SpeechNexus/issues/33)、[C01 #36](https://github.com/hrygo/SpeechNexus/issues/36) |
| [S4 #11](https://github.com/hrygo/SpeechNexus/issues/11) [Epic S4] 会议、提词、助手与有声书 | [G4 #17](https://github.com/hrygo/SpeechNexus/issues/17) 实施中 | [C05 #40](https://github.com/hrygo/SpeechNexus/issues/40)、[C06 #41](https://github.com/hrygo/SpeechNexus/issues/41)、[C07 #42](https://github.com/hrygo/SpeechNexus/issues/42) |
| [S5 #12](https://github.com/hrygo/SpeechNexus/issues/12) [Epic S5] 目标拓扑、恢复与完整交付资格 | [G5 #18](https://github.com/hrygo/SpeechNexus/issues/18) 待实施 | [A07 #26](https://github.com/hrygo/SpeechNexus/issues/26)、[B07 #34](https://github.com/hrygo/SpeechNexus/issues/34)、[C08 #43](https://github.com/hrygo/SpeechNexus/issues/43) |

原生层级为总控→6 Epic→工作包/Gate。工作包按完整范围唯一归属，跨阶段检查项在下面单独核对；阶段推进看 Gate，不等待前一 Epic 全部关闭。

### Gate 证据与原生阻塞

| Gate | 必须有的证据 | 真正的整包/关口前置 |
| --- | --- | --- |
| [G0 #13](https://github.com/hrygo/SpeechNexus/issues/13) | 三队消费的生成类型/正负例；18 端口所有权及缺口表；首个生产事务；引擎/硬件/许可/停止探针结论；冻结首批语料与浏览器矩阵 | [A00 #19](https://github.com/hrygo/SpeechNexus/issues/19)、[B00 #27](https://github.com/hrygo/SpeechNexus/issues/27)、[C00 #35](https://github.com/hrygo/SpeechNexus/issues/35) |
| [G1 #14](https://github.com/hrygo/SpeechNexus/issues/14) | 无 Web 的标准 SDK 文件 ASR/TTS；上传→Job→原子准入→唯一外发→staging/发布→下载；基本页面；进程重启/取消/同键重放；基础删除 | [G0 #13](https://github.com/hrygo/SpeechNexus/issues/13) |
| [G2 #15](https://github.com/hrygo/SpeechNexus/issues/15) | 实时 ASR/TTS 的样本/文本水位与背压；可信 abort/stop；设计和克隆各自的候选→复用→发布；加载/回收/unknown 证据 | [G1 #14](https://github.com/hrygo/SpeechNexus/issues/14) |
| [G3 #16](https://github.com/hrygo/SpeechNexus/issues/16) | 四类语音与至少一条真实可复用音色；全部 18 生产端口/完整快照；双库协调行为；最低 1 路实时 ASR+1 路实时 TTS 与 VoiceDesign 共存；基础管理/工作台；对应拓扑的试点资格；无状态 MCP 工具发现/调用及 REST 等价行为；预设合并/精确参数拒绝、发布资格/CAS、默认指针回滚与词典版本 | [G2 #15](https://github.com/hrygo/SpeechNexus/issues/15) |
| [G4 #17](https://github.com/hrygo/SpeechNexus/issues/17) | 会议封存/校对、提词手动接管、助手插话取消、版本化有声书局部重做/完整导出；各自删除/恢复；LLM 启用时通过内网内容边界；会议要点版本化保存、有声书质量/试听/匿名确认记录 | [G3 #16](https://github.com/hrygo/SpeechNexus/issues/16) |
| [G5 #18](https://github.com/hrygo/SpeechNexus/issues/18) | 物理多主机、网络分区/节点丢失、旧入口隔离、目标数据层切换/备份恢复、迁移/回退、长时/质量/容量、浏览器及内容出站、许可/交接资料完整；SDK/MCP 可运行交付、项目许可决策及冻结 RPO/RTO 的恢复证据 | [G4 #17](https://github.com/hrygo/SpeechNexus/issues/17) |

除 G0 的 A00/B00/C00 基础输入及 G0→G5 关口顺序外，不把部分制品或联合联调错误地建成整包 blocked_by。A01/A07/B07/C08 等包持续 open 时，已具备证据的早期 Gate 可以放行。

## 25 个工作包与依赖矩阵

| 工作包 | 团队/主 Epic | 状态 | 开工所需制品（分段，不要求整包先关闭） | 联合验收伙伴 | Gate 检查项 |
| --- | --- | --- | --- | --- | --- |
| [A00 #19](https://github.com/hrygo/SpeechNexus/issues/19) [A00] 契约类型生成、语义样例与接口交接 | A / [S0 #7](https://github.com/hrygo/SpeechNexus/issues/7) | 实施中 | 现有契约/环境可开始；外部条件按正文核对 | — | [G0 #13](https://github.com/hrygo/SpeechNexus/issues/13) |
| [A01 #20](https://github.com/hrygo/SpeechNexus/issues/20) [A01] 生产协调事务、迁移权限与 18 端口验收 | A / [S3 #10](https://github.com/hrygo/SpeechNexus/issues/10) | 实施中 | [A00 #19](https://github.com/hrygo/SpeechNexus/issues/19)：相应端口/生成类型/正负样例子包 | [B01 #28](https://github.com/hrygo/SpeechNexus/issues/28)、[B04 #31](https://github.com/hrygo/SpeechNexus/issues/31) | [G0 #13](https://github.com/hrygo/SpeechNexus/issues/13)、[G1 #14](https://github.com/hrygo/SpeechNexus/issues/14)、[G2 #15](https://github.com/hrygo/SpeechNexus/issues/15)、[G3 #16](https://github.com/hrygo/SpeechNexus/issues/16) |
| [A02 #21](https://github.com/hrygo/SpeechNexus/issues/21) [A02] 资产、不可变版本与对象原子发布 | A / [S1 #8](https://github.com/hrygo/SpeechNexus/issues/8) | 实施中 | [A00 #19](https://github.com/hrygo/SpeechNexus/issues/19)：资产/版本/receipt 类型；[A01 #20](https://github.com/hrygo/SpeechNexus/issues/20)：基础迁移与角色子包 | [B03 #30](https://github.com/hrygo/SpeechNexus/issues/30)、[A07 #26](https://github.com/hrygo/SpeechNexus/issues/26) | [G1 #14](https://github.com/hrygo/SpeechNexus/issues/14)、[G3 #16](https://github.com/hrygo/SpeechNexus/issues/16) |
| [A03 #22](https://github.com/hrygo/SpeechNexus/issues/22) [A03] 调度准入、持久任务、配额与未知恢复 | A / [S1 #8](https://github.com/hrygo/SpeechNexus/issues/8) | 实施中 | [A01 #20](https://github.com/hrygo/SpeechNexus/issues/20)：owner/admission/job/runtime 端口与迁移 | [B01 #28](https://github.com/hrygo/SpeechNexus/issues/28)、[B02 #29](https://github.com/hrygo/SpeechNexus/issues/29) | [G1 #14](https://github.com/hrygo/SpeechNexus/issues/14)、[G3 #16](https://github.com/hrygo/SpeechNexus/issues/16) |
| [A04 #23](https://github.com/hrygo/SpeechNexus/issues/23) [A04] 标准文件语音、任务与管理 REST/SSE | A / [S1 #8](https://github.com/hrygo/SpeechNexus/issues/8) | 实施中 | [A02 #21](https://github.com/hrygo/SpeechNexus/issues/21)：资源引用/版本/发布端口；[A03 #22](https://github.com/hrygo/SpeechNexus/issues/22)：准入/Job/Operation 端口 | [B03 #30](https://github.com/hrygo/SpeechNexus/issues/30)、[C01 #36](https://github.com/hrygo/SpeechNexus/issues/36)、[C02 #37](https://github.com/hrygo/SpeechNexus/issues/37) | [G1 #14](https://github.com/hrygo/SpeechNexus/issues/14)、[G3 #16](https://github.com/hrygo/SpeechNexus/issues/16) |
| [A05 #24](https://github.com/hrygo/SpeechNexus/issues/24) [A05] 公开实时协议、双工准入与流恢复 | A / [S2 #9](https://github.com/hrygo/SpeechNexus/issues/9) | 待实施 | [A04 #23](https://github.com/hrygo/SpeechNexus/issues/23)：公开规范命令与持久恢复基础 | [B04 #31](https://github.com/hrygo/SpeechNexus/issues/31)、[C04 #39](https://github.com/hrygo/SpeechNexus/issues/39) | [G2 #15](https://github.com/hrygo/SpeechNexus/issues/15) |
| [A06 #25](https://github.com/hrygo/SpeechNexus/issues/25) [A06] 音色候选、复用校验与版本发布 | A / [S2 #9](https://github.com/hrygo/SpeechNexus/issues/9) | 待实施 | [A02 #21](https://github.com/hrygo/SpeechNexus/issues/21)：材料/版本/发布基础；[A04 #23](https://github.com/hrygo/SpeechNexus/issues/23)：规范命令/任务基础 | [B05 #32](https://github.com/hrygo/SpeechNexus/issues/32)、[C03 #38](https://github.com/hrygo/SpeechNexus/issues/38) | [G2 #15](https://github.com/hrygo/SpeechNexus/issues/15) |
| [A07 #26](https://github.com/hrygo/SpeechNexus/issues/26) [A07] 删除账本、备份恢复与平台发布交付 | A / [S5 #12](https://github.com/hrygo/SpeechNexus/issues/12) | 实施中 | [A02 #21](https://github.com/hrygo/SpeechNexus/issues/21)：首个持久资源与依赖身份 | [B01 #28](https://github.com/hrygo/SpeechNexus/issues/28)、[B02 #29](https://github.com/hrygo/SpeechNexus/issues/29)、[C00 #35](https://github.com/hrygo/SpeechNexus/issues/35)、[B07 #34](https://github.com/hrygo/SpeechNexus/issues/34)、[C08 #43](https://github.com/hrygo/SpeechNexus/issues/43) | [G1 #14](https://github.com/hrygo/SpeechNexus/issues/14)、[G2 #15](https://github.com/hrygo/SpeechNexus/issues/15)、[G3 #16](https://github.com/hrygo/SpeechNexus/issues/16)、[G5 #18](https://github.com/hrygo/SpeechNexus/issues/18) |
| [B00 #27](https://github.com/hrygo/SpeechNexus/issues/27) [B00] MegaCube 环境、模型可行性与停止证据探针 | B / [S0 #7](https://github.com/hrygo/SpeechNexus/issues/7) | 实施中 | 现有契约/环境可开始；外部条件按正文核对 | — | [G0 #13](https://github.com/hrygo/SpeechNexus/issues/13) |
| [B01 #28](https://github.com/hrygo/SpeechNexus/issues/28) [B01] 执行网关、持久意图与真实提交屏障 | B / [S1 #8](https://github.com/hrygo/SpeechNexus/issues/8) | 实施中 | [A00 #19](https://github.com/hrygo/SpeechNexus/issues/19)：内部执行/错误/时序样例；[A01 #20](https://github.com/hrygo/SpeechNexus/issues/20)：网关表迁移/最小角色/租约查询子包 | [A03 #22](https://github.com/hrygo/SpeechNexus/issues/22)、[B02 #29](https://github.com/hrygo/SpeechNexus/issues/29) | [G0 #13](https://github.com/hrygo/SpeechNexus/issues/13)、[G1 #14](https://github.com/hrygo/SpeechNexus/issues/14) |
| [B02 #29](https://github.com/hrygo/SpeechNexus/issues/29) [B02] 受限节点控制器与实例生命周期 | B / [S2 #9](https://github.com/hrygo/SpeechNexus/issues/9) | 实施中 | [A00 #19](https://github.com/hrygo/SpeechNexus/issues/19)：runtime 协议/样例；[A01 #20](https://github.com/hrygo/SpeechNexus/issues/20)：runtime 意图/观察与 generation 端口 | [A03 #22](https://github.com/hrygo/SpeechNexus/issues/22)、[B01 #28](https://github.com/hrygo/SpeechNexus/issues/28)、[B03 #30](https://github.com/hrygo/SpeechNexus/issues/30) | [G0 #13](https://github.com/hrygo/SpeechNexus/issues/13)、[G1 #14](https://github.com/hrygo/SpeechNexus/issues/14)、[G2 #15](https://github.com/hrygo/SpeechNexus/issues/15) |
| [B03 #30](https://github.com/hrygo/SpeechNexus/issues/30) [B03] 真实文件 ASR/TTS 与有界媒体流水线 | B / [S1 #8](https://github.com/hrygo/SpeechNexus/issues/8) | 实施中 | [B00 #27](https://github.com/hrygo/SpeechNexus/issues/27)：批准 binding/模型/镜像及真实能力探针；[B01 #28](https://github.com/hrygo/SpeechNexus/issues/28)：持久 intent/dispatch/stop 子包 | [A02 #21](https://github.com/hrygo/SpeechNexus/issues/21)、[A03 #22](https://github.com/hrygo/SpeechNexus/issues/22)、[A04 #23](https://github.com/hrygo/SpeechNexus/issues/23)、[B02 #29](https://github.com/hrygo/SpeechNexus/issues/29)、[C02 #37](https://github.com/hrygo/SpeechNexus/issues/37) | [G1 #14](https://github.com/hrygo/SpeechNexus/issues/14) |
| [B04 #31](https://github.com/hrygo/SpeechNexus/issues/31) [B04] 流式 ASR/TTS、PCM 背压与输出区间账本 | B / [S2 #9](https://github.com/hrygo/SpeechNexus/issues/9) | 实施中 | [B01 #28](https://github.com/hrygo/SpeechNexus/issues/28)：内部流/执行身份与停止基础；[B03 #30](https://github.com/hrygo/SpeechNexus/issues/30)：媒体映射/ASR/TTS adapter 基础；[B06 #33](https://github.com/hrygo/SpeechNexus/issues/33)：VAD 子项；required 辅助完整配方 | [A05 #24](https://github.com/hrygo/SpeechNexus/issues/24)、[C04 #39](https://github.com/hrygo/SpeechNexus/issues/39)、[B02 #29](https://github.com/hrygo/SpeechNexus/issues/29) | [G2 #15](https://github.com/hrygo/SpeechNexus/issues/15) |
| [B05 #32](https://github.com/hrygo/SpeechNexus/issues/32) [B05] VoiceDesign/Base、克隆与声音材料恢复 | B / [S2 #9](https://github.com/hrygo/SpeechNexus/issues/9) | 待实施 | [B00 #27](https://github.com/hrygo/SpeechNexus/issues/27)：VoiceDesign/Base 关键可行性；[B02 #29](https://github.com/hrygo/SpeechNexus/issues/29)：prepare/ready 与受控回收子项；[B03 #30](https://github.com/hrygo/SpeechNexus/issues/30)：Base/媒体基础 | [A06 #25](https://github.com/hrygo/SpeechNexus/issues/25)、[C03 #38](https://github.com/hrygo/SpeechNexus/issues/38) | [G2 #15](https://github.com/hrygo/SpeechNexus/issues/15) |
| [B06 #33](https://github.com/hrygo/SpeechNexus/issues/33) [B06] VAD、对齐与匿名分人能力 | B / [S3 #10](https://github.com/hrygo/SpeechNexus/issues/10) | 实施中 | 现有契约/环境可开始；外部条件按正文核对 | [B03 #30](https://github.com/hrygo/SpeechNexus/issues/30)、[B04 #31](https://github.com/hrygo/SpeechNexus/issues/31)、[C05 #40](https://github.com/hrygo/SpeechNexus/issues/40) | [G1 #14](https://github.com/hrygo/SpeechNexus/issues/14)、[G2 #15](https://github.com/hrygo/SpeechNexus/issues/15)、[G3 #16](https://github.com/hrygo/SpeechNexus/issues/16) |
| [B07 #34](https://github.com/hrygo/SpeechNexus/issues/34) [B07] 模型部署、混合负载与目标拓扑资格 | B / [S5 #12](https://github.com/hrygo/SpeechNexus/issues/12) | 待实施 | [B00 #27](https://github.com/hrygo/SpeechNexus/issues/27)：目标环境/版本/许可与门槛草案 | [A07 #26](https://github.com/hrygo/SpeechNexus/issues/26)、[C08 #43](https://github.com/hrygo/SpeechNexus/issues/43)、[B03 #30](https://github.com/hrygo/SpeechNexus/issues/30)、[B04 #31](https://github.com/hrygo/SpeechNexus/issues/31)、[B05 #32](https://github.com/hrygo/SpeechNexus/issues/32) | [G0 #13](https://github.com/hrygo/SpeechNexus/issues/13)、[G1 #14](https://github.com/hrygo/SpeechNexus/issues/14)、[G2 #15](https://github.com/hrygo/SpeechNexus/issues/15)、[G3 #16](https://github.com/hrygo/SpeechNexus/issues/16)、[G5 #18](https://github.com/hrygo/SpeechNexus/issues/18) |
| [C00 #35](https://github.com/hrygo/SpeechNexus/issues/35) [C00] 工作台版本骨架、契约客户端与开发 fake | C / [S0 #7](https://github.com/hrygo/SpeechNexus/issues/7) | 实施中 | 现有契约/环境可开始；外部条件按正文核对 | [A00 #19](https://github.com/hrygo/SpeechNexus/issues/19)、[A07 #26](https://github.com/hrygo/SpeechNexus/issues/26) | [G0 #13](https://github.com/hrygo/SpeechNexus/issues/13) |
| [C01 #36](https://github.com/hrygo/SpeechNexus/issues/36) [C01] AdminUI 与持久任务中心 | C / [S3 #10](https://github.com/hrygo/SpeechNexus/issues/10) | 实施中 | [C00 #35](https://github.com/hrygo/SpeechNexus/issues/35)：客户端/fake/交互骨架 | [A04 #23](https://github.com/hrygo/SpeechNexus/issues/23)、[A06 #25](https://github.com/hrygo/SpeechNexus/issues/25)、[B02 #29](https://github.com/hrygo/SpeechNexus/issues/29) | [G1 #14](https://github.com/hrygo/SpeechNexus/issues/14)、[G3 #16](https://github.com/hrygo/SpeechNexus/issues/16) |
| [C02 #37](https://github.com/hrygo/SpeechNexus/issues/37) [C02] 转写校对导出与文本配音闭环 | C / [S1 #8](https://github.com/hrygo/SpeechNexus/issues/8) | 实施中 | [C00 #35](https://github.com/hrygo/SpeechNexus/issues/35)：项目 revision/生成客户端/fake | [A02 #21](https://github.com/hrygo/SpeechNexus/issues/21)、[A04 #23](https://github.com/hrygo/SpeechNexus/issues/23)、[B03 #30](https://github.com/hrygo/SpeechNexus/issues/30) | [G1 #14](https://github.com/hrygo/SpeechNexus/issues/14) |
| [C03 #38](https://github.com/hrygo/SpeechNexus/issues/38) [C03] 音色创作、校验发布与复用工作室 | C / [S2 #9](https://github.com/hrygo/SpeechNexus/issues/9) | 实施中 | [C00 #35](https://github.com/hrygo/SpeechNexus/issues/35)：客户端/候选交互骨架 | [A06 #25](https://github.com/hrygo/SpeechNexus/issues/25)、[B05 #32](https://github.com/hrygo/SpeechNexus/issues/32) | [G2 #15](https://github.com/hrygo/SpeechNexus/issues/15) |
| [C04 #39](https://github.com/hrygo/SpeechNexus/issues/39) [C04] 浏览器采集播放、实时字幕与媒体生命周期 | C / [S2 #9](https://github.com/hrygo/SpeechNexus/issues/9) | 实施中 | [C00 #35](https://github.com/hrygo/SpeechNexus/issues/35)：事件/交互契约基础 | [A05 #24](https://github.com/hrygo/SpeechNexus/issues/24)、[B04 #31](https://github.com/hrygo/SpeechNexus/issues/31)、[B06 #33](https://github.com/hrygo/SpeechNexus/issues/33) | [G0 #13](https://github.com/hrygo/SpeechNexus/issues/13)、[G2 #15](https://github.com/hrygo/SpeechNexus/issues/15) |
| [C05 #40](https://github.com/hrygo/SpeechNexus/issues/40) [C05] 会议封存、校对与提词跟随 | C / [S4 #11](https://github.com/hrygo/SpeechNexus/issues/11) | 实施中 | [C02 #37](https://github.com/hrygo/SpeechNexus/issues/37)：转写/校对/版本基础；[C04 #39](https://github.com/hrygo/SpeechNexus/issues/39)：真实字幕/媒体生命周期 | [B06 #33](https://github.com/hrygo/SpeechNexus/issues/33) | [G4 #17](https://github.com/hrygo/SpeechNexus/issues/17) |
| [C06 #41](https://github.com/hrygo/SpeechNexus/issues/41) [C06] 内网 LLM、会议纪要与语音助手 | C / [S4 #11](https://github.com/hrygo/SpeechNexus/issues/11) | 实施中 | [C00 #35](https://github.com/hrygo/SpeechNexus/issues/35)：LLM fake/版本基础；[C05 #40](https://github.com/hrygo/SpeechNexus/issues/40)：封存转写；纪要真实接入；[C02 #37](https://github.com/hrygo/SpeechNexus/issues/37)：TTS/文本冻结；助手接入；[C04 #39](https://github.com/hrygo/SpeechNexus/issues/39)：双工/播放 generation；助手接入 | [A05 #24](https://github.com/hrygo/SpeechNexus/issues/24)、[B04 #31](https://github.com/hrygo/SpeechNexus/issues/31)、[A07 #26](https://github.com/hrygo/SpeechNexus/issues/26)、[B07 #34](https://github.com/hrygo/SpeechNexus/issues/34) | [G4 #17](https://github.com/hrygo/SpeechNexus/issues/17) |
| [C07 #42](https://github.com/hrygo/SpeechNexus/issues/42) [C07] 有声书局部重做、保存副本与独立导出 | C / [S4 #11](https://github.com/hrygo/SpeechNexus/issues/11) | 实施中 | [C00 #35](https://github.com/hrygo/SpeechNexus/issues/35)：项目/稿件版本与独立存储；[C02 #37](https://github.com/hrygo/SpeechNexus/issues/37)：配音/结果消费基础；[A02 #21](https://github.com/hrygo/SpeechNexus/issues/21)：Artifact/receipt 下载与版本；[A04 #23](https://github.com/hrygo/SpeechNexus/issues/23)：持久任务/语音 API；[C03 #38](https://github.com/hrygo/SpeechNexus/issues/38)：若使用创作声音则需发布/复用子项 | — | [G4 #17](https://github.com/hrygo/SpeechNexus/issues/17) |
| [C08 #43](https://github.com/hrygo/SpeechNexus/issues/43) [C08] 浏览器、业务删除恢复与应用交付资格 | C / [S5 #12](https://github.com/hrygo/SpeechNexus/issues/12) | 实施中 | [C00 #35](https://github.com/hrygo/SpeechNexus/issues/35)：目标浏览器/业务骨架 | [A07 #26](https://github.com/hrygo/SpeechNexus/issues/26)、[B07 #34](https://github.com/hrygo/SpeechNexus/issues/34)、[C01 #36](https://github.com/hrygo/SpeechNexus/issues/36)、[C02 #37](https://github.com/hrygo/SpeechNexus/issues/37)、[C03 #38](https://github.com/hrygo/SpeechNexus/issues/38)、[C04 #39](https://github.com/hrygo/SpeechNexus/issues/39)、[C05 #40](https://github.com/hrygo/SpeechNexus/issues/40)、[C06 #41](https://github.com/hrygo/SpeechNexus/issues/41)、[C07 #42](https://github.com/hrygo/SpeechNexus/issues/42) | [G0 #13](https://github.com/hrygo/SpeechNexus/issues/13)、[G1 #14](https://github.com/hrygo/SpeechNexus/issues/14)、[G2 #15](https://github.com/hrygo/SpeechNexus/issues/15)、[G3 #16](https://github.com/hrygo/SpeechNexus/issues/16)、[G4 #17](https://github.com/hrygo/SpeechNexus/issues/17)、[G5 #18](https://github.com/hrygo/SpeechNexus/issues/18) |

## 跨阶段证据矩阵

每个格子的证据在对应工作包正文检查项与 Gate 中回填链接/集成 SHA；此表描述应交付内容，不自动把 issue closed 或 PR 合并转换为证据。

| Gate | 各队必须交接的具名检查项 |
| --- | --- |
| [G0 #13](https://github.com/hrygo/SpeechNexus/issues/13) | [A00 #19](https://github.com/hrygo/SpeechNexus/issues/19)：生成/校验、责任矩阵、首闭环样例和消费者确认；[A01 #20](https://github.com/hrygo/SpeechNexus/issues/20)：签名/样例、首个生产事务与迁移角色；[B00 #27](https://github.com/hrygo/SpeechNexus/issues/27)：环境/许可/关键模型与 stop 可行性结论；[B01 #28](https://github.com/hrygo/SpeechNexus/issues/28)：fake 故障与协议子包；[B02 #29](https://github.com/hrygo/SpeechNexus/issues/29)：controller fake/generation 接口；[B07 #34](https://github.com/hrygo/SpeechNexus/issues/34)：资格模板/门槛/环境窗口；[C00 #35](https://github.com/hrygo/SpeechNexus/issues/35)：客户端/项目版本骨架/开发 fake 与接口确认；[C04 #39](https://github.com/hrygo/SpeechNexus/issues/39)：合成媒体/fake WS 探针；[C08 #43](https://github.com/hrygo/SpeechNexus/issues/43)：目标浏览器/安全上下文矩阵 |
| [G1 #14](https://github.com/hrygo/SpeechNexus/issues/14) | [A01 #20](https://github.com/hrygo/SpeechNexus/issues/20)：文件闭环所用生产端口；[A02 #21](https://github.com/hrygo/SpeechNexus/issues/21)：资产/版本/receipt/staging/发布与下载；[A03 #22](https://github.com/hrygo/SpeechNexus/issues/22)：持久 Job/outbox、完整文件准入和未知恢复；[A04 #23](https://github.com/hrygo/SpeechNexus/issues/23)：标准文件 SDK/API/SSE 与页面消费；[A07 #26](https://github.com/hrygo/SpeechNexus/issues/26)：首个资源基础删除及 tombstone；[B01 #28](https://github.com/hrygo/SpeechNexus/issues/28)：唯一 intent、真实屏障、unknown/stop 与文件执行；[B02 #29](https://github.com/hrygo/SpeechNexus/issues/29)：prepare/ready 与文件实例准备；[B03 #30](https://github.com/hrygo/SpeechNexus/issues/30)：真实文件 ASR/TTS 和有界媒体/格式/取消；[B06 #33](https://github.com/hrygo/SpeechNexus/issues/33)：实时开发前 VAD 子项；[B07 #34](https://github.com/hrygo/SpeechNexus/issues/34)：文件真实资格；[C01 #36](https://github.com/hrygo/SpeechNexus/issues/36)：任务/基础管理真实消费者；[C02 #37](https://github.com/hrygo/SpeechNexus/issues/37)：真实转写校对导出与配音试听下载；[C08 #43](https://github.com/hrygo/SpeechNexus/issues/43)：基础页面恢复与下载 |
| [G2 #15](https://github.com/hrygo/SpeechNexus/issues/15) | [A01 #20](https://github.com/hrygo/SpeechNexus/issues/20)：runtime 生命周期/流计量协同；[A05 #24](https://github.com/hrygo/SpeechNexus/issues/24)：实时/双工/owner/epoch/水位/背压/恢复；[A06 #25](https://github.com/hrygo/SpeechNexus/issues/25)：候选、换文复用、发布与退役/禁用竞争；[A07 #26](https://github.com/hrygo/SpeechNexus/issues/26)：音色/网关命令正文受控清理；[B02 #29](https://github.com/hrygo/SpeechNexus/issues/29)：完整 peak/steady/drain/停止/释放；[B04 #31](https://github.com/hrygo/SpeechNexus/issues/31)：真实流式、credit、输出区间与硬上界；[B05 #32](https://github.com/hrygo/SpeechNexus/issues/32)：设计/克隆、换文复用、重启材料恢复；[B06 #33](https://github.com/hrygo/SpeechNexus/issues/33)：required 辅助完整配方；[B07 #34](https://github.com/hrygo/SpeechNexus/issues/34)：实时/音色真实资格；[C03 #38](https://github.com/hrygo/SpeechNexus/issues/38)：音色工作室真实创作/校验/发布/复用；[C04 #39](https://github.com/hrygo/SpeechNexus/issues/39)：真实设备/远端实时与生命周期；[C08 #43](https://github.com/hrygo/SpeechNexus/issues/43)：真实设备/实时/音色交互 |
| [G3 #16](https://github.com/hrygo/SpeechNexus/issues/16) | [A01 #20](https://github.com/hrygo/SpeechNexus/issues/20)：全部 18 端口、完整快照与双库无跳过；[A02 #21](https://github.com/hrygo/SpeechNexus/issues/21)：预设合并/override/exact、发布资格/CAS/默认指针回滚与不可变词典版本；[A03 #22](https://github.com/hrygo/SpeechNexus/issues/22)：双工/跨池/全部 quota 与结算证据；[A04 #23](https://github.com/hrygo/SpeechNexus/issues/23)：基础管理面全部有效能力；MCP 工具发现/调用与公开 REST 同命令/错误/幂等/取消；[A07 #26](https://github.com/hrygo/SpeechNexus/issues/26)：删除与恢复/未知预算联验；[B06 #33](https://github.com/hrygo/SpeechNexus/issues/33)：分人/对齐支持矩阵与有界连续性；[B07 #34](https://github.com/hrygo/SpeechNexus/issues/34)：1+1/VoiceDesign 混合负载；[C01 #36](https://github.com/hrygo/SpeechNexus/issues/36)：完整基础 Admin/策略/实例/诊断；预设校验/发布/回滚及 CAS 冲突可操作；[C08 #43](https://github.com/hrygo/SpeechNexus/issues/43)：基础产品体验 |
| [G4 #17](https://github.com/hrygo/SpeechNexus/issues/17) | [C05 #40](https://github.com/hrygo/SpeechNexus/issues/40)：会议要点标记/封存/校对/导出与提词手动接管；[C06 #41](https://github.com/hrygo/SpeechNexus/issues/41)：内网 LLM/纪要/助手插话与内容边界；[C07 #42](https://github.com/hrygo/SpeechNexus/issues/42)：局部重做/保存副本/独立完整导出；独立质量/试听/匿名确认记录；[C08 #43](https://github.com/hrygo/SpeechNexus/issues/43)：高级流程/内容生命周期 |
| [G5 #18](https://github.com/hrygo/SpeechNexus/issues/18) | [A07 #26](https://github.com/hrygo/SpeechNexus/issues/26)：迁移/备份恢复/唯一权威/发布包，SDK/MCP 配置与示例、许可决策、冻结 RPO/RTO 的恢复实测；[B07 #34](https://github.com/hrygo/SpeechNexus/issues/34)：目标拓扑/故障/长时/完整发布资格；[C08 #43](https://github.com/hrygo/SpeechNexus/issues/43)：完整浏览器/删除恢复/文档交接 |

## 整体产品交付覆盖矩阵

每项产品能力必须由对应工作包提供证据，在列明的 Gate 验收。此表用于范围追踪，不能用映射存在证明实施完成。

| 范围 | 主责 | 实施工作包 | 验收 Gate | 必须交付的证据 |
| --- | --- | --- | --- | --- |
| D01 契约、生成客户端与兼容子集 | A | [A00 #19](https://github.com/hrygo/SpeechNexus/issues/19)、[A04 #23](https://github.com/hrygo/SpeechNexus/issues/23)、[A05 #24](https://github.com/hrygo/SpeechNexus/issues/24) | [G0 #13](https://github.com/hrygo/SpeechNexus/issues/13)、[G1 #14](https://github.com/hrygo/SpeechNexus/issues/14)、[G3 #16](https://github.com/hrygo/SpeechNexus/issues/16) | OpenAPI/实时 schema、生成类型、官方 SDK 与真实 HTTP/WS 正负例 |
| D02 无状态 MCP 代理与 SDK 接入交付 | A | [A00 #19](https://github.com/hrygo/SpeechNexus/issues/19)、[A04 #23](https://github.com/hrygo/SpeechNexus/issues/23)、[A07 #26](https://github.com/hrygo/SpeechNexus/issues/26) | [G3 #16](https://github.com/hrygo/SpeechNexus/issues/16)、[G5 #18](https://github.com/hrygo/SpeechNexus/issues/18) | 工具发现/调用、REST 同命令同错误/幂等/取消及可运行安装配置/示例 |
| D03 文件 ASR 与完整文本 TTS | A/B/C | [A02 #21](https://github.com/hrygo/SpeechNexus/issues/21)、[A03 #22](https://github.com/hrygo/SpeechNexus/issues/22)、[A04 #23](https://github.com/hrygo/SpeechNexus/issues/23)、[B01 #28](https://github.com/hrygo/SpeechNexus/issues/28)、[B03 #30](https://github.com/hrygo/SpeechNexus/issues/30)、[C02 #37](https://github.com/hrygo/SpeechNexus/issues/37) | [G1 #14](https://github.com/hrygo/SpeechNexus/issues/14) | 真实标准 SDK/页面、采样映射、MP3/WAV/PCM、取消恢复与制品发布 |
| D04 实时 ASR/TTS、字幕与双工 | A/B/C | [A05 #24](https://github.com/hrygo/SpeechNexus/issues/24)、[B04 #31](https://github.com/hrygo/SpeechNexus/issues/31)、[B06 #33](https://github.com/hrygo/SpeechNexus/issues/33)、[C04 #39](https://github.com/hrygo/SpeechNexus/issues/39) | [G2 #15](https://github.com/hrygo/SpeechNexus/issues/15)、[G3 #16](https://github.com/hrygo/SpeechNexus/issues/16) | owner/epoch/gap/credit、完整配方准入、可信 stop、真实设备与样本区间 |
| D05 音色设计、克隆、校验发布及复用 | A/B/C | [A06 #25](https://github.com/hrygo/SpeechNexus/issues/25)、[B02 #29](https://github.com/hrygo/SpeechNexus/issues/29)、[B05 #32](https://github.com/hrygo/SpeechNexus/issues/32)、[C03 #38](https://github.com/hrygo/SpeechNexus/issues/38) | [G2 #15](https://github.com/hrygo/SpeechNexus/issues/15)、[G3 #16](https://github.com/hrygo/SpeechNexus/issues/16) | 设计/克隆分别验证；换文复用→显式发布→后续 TTS；重启材料恢复 |
| D06 预设、发音资源与不可变版本 | A/C | [A02 #21](https://github.com/hrygo/SpeechNexus/issues/21)、[A04 #23](https://github.com/hrygo/SpeechNexus/issues/23)、[A06 #25](https://github.com/hrygo/SpeechNexus/issues/25)、[C01 #36](https://github.com/hrygo/SpeechNexus/issues/36)、[C07 #42](https://github.com/hrygo/SpeechNexus/issues/42) | [G1 #14](https://github.com/hrygo/SpeechNexus/issues/14)、[G3 #16](https://github.com/hrygo/SpeechNexus/issues/16)、[G4 #17](https://github.com/hrygo/SpeechNexus/issues/17) | 合并顺序/allowlist/ranges/exact、发布资格/CAS、默认指针回滚不改旧 Job、预设/词典公开端点与固定 revision |
| D07 持久任务、完整预算与计量 | A/B | [A01 #20](https://github.com/hrygo/SpeechNexus/issues/20)、[A03 #22](https://github.com/hrygo/SpeechNexus/issues/22)、[B01 #28](https://github.com/hrygo/SpeechNexus/issues/28)、[B02 #29](https://github.com/hrygo/SpeechNexus/issues/29)、[B04 #31](https://github.com/hrygo/SpeechNexus/issues/31) | [G1 #14](https://github.com/hrygo/SpeechNexus/issues/14)、[G2 #15](https://github.com/hrygo/SpeechNexus/issues/15)、[G3 #16](https://github.com/hrygo/SpeechNexus/issues/16) | 18 端口/完整快照、单事务准入、双库协调、跨池父预算、unknown 不提前释放 |
| D08 工作台项目、转写校对与配音 | A/C | [A02 #21](https://github.com/hrygo/SpeechNexus/issues/21)、[A04 #23](https://github.com/hrygo/SpeechNexus/issues/23)、[C00 #35](https://github.com/hrygo/SpeechNexus/issues/35)、[C02 #37](https://github.com/hrygo/SpeechNexus/issues/37) | [G1 #14](https://github.com/hrygo/SpeechNexus/issues/14)、[G3 #16](https://github.com/hrygo/SpeechNexus/issues/16) | 独立 schema/版本、原文/校对分层、持久恢复、公开 API/receipt 消费 |
| D09 Admin、模型/实例/策略/任务与诊断 | A/B/C | [A04 #23](https://github.com/hrygo/SpeechNexus/issues/23)、[A06 #25](https://github.com/hrygo/SpeechNexus/issues/25)、[B02 #29](https://github.com/hrygo/SpeechNexus/issues/29)、[C01 #36](https://github.com/hrygo/SpeechNexus/issues/36) | [G1 #14](https://github.com/hrygo/SpeechNexus/issues/14)、[G3 #16](https://github.com/hrygo/SpeechNexus/issues/16) | 真实能力/资格/就绪、管理写路径版本/确认、任务恢复与共享边界 |
| D10 VAD、对齐与匿名分人 | B/C | [B06 #33](https://github.com/hrygo/SpeechNexus/issues/33)、[C04 #39](https://github.com/hrygo/SpeechNexus/issues/39)、[C05 #40](https://github.com/hrygo/SpeechNexus/issues/40) | [G2 #15](https://github.com/hrygo/SpeechNexus/issues/15)、[G3 #16](https://github.com/hrygo/SpeechNexus/issues/16)、[G4 #17](https://github.com/hrygo/SpeechNexus/issues/17) | 有界会话、匹配正文 revision、跨块连续/重叠/reset、required 完整准入 |
| D11 会议、封存导出与提词 | B/C | [B06 #33](https://github.com/hrygo/SpeechNexus/issues/33)、[C02 #37](https://github.com/hrygo/SpeechNexus/issues/37)、[C04 #39](https://github.com/hrygo/SpeechNexus/issues/39)、[C05 #40](https://github.com/hrygo/SpeechNexus/issues/40) | [G4 #17](https://github.com/hrygo/SpeechNexus/issues/17) | 要点标记/版本保存、sealed 转写/校对、来源/gap、旧稿/旧 epoch 隔离、手动接管 |
| D12 内网 LLM、纪要与语音助手 | A/B/C | [A05 #24](https://github.com/hrygo/SpeechNexus/issues/24)、[B04 #31](https://github.com/hrygo/SpeechNexus/issues/31)、[B07 #34](https://github.com/hrygo/SpeechNexus/issues/34)、[C06 #41](https://github.com/hrygo/SpeechNexus/issues/41)、[C08 #43](https://github.com/hrygo/SpeechNexus/issues/43) | [G4 #17](https://github.com/hrygo/SpeechNexus/issues/17)、[G5 #18](https://github.com/hrygo/SpeechNexus/issues/18) | 默认关闭、批准内网/无外网回退、固定版本、turn/插话/迟到音频隔离 |
| D13 有声书、保存副本与独立导出 | A/C | [A02 #21](https://github.com/hrygo/SpeechNexus/issues/21)、[A04 #23](https://github.com/hrygo/SpeechNexus/issues/23)、[C02 #37](https://github.com/hrygo/SpeechNexus/issues/37)、[C03 #38](https://github.com/hrygo/SpeechNexus/issues/38)、[C07 #42](https://github.com/hrygo/SpeechNexus/issues/42) | [G4 #17](https://github.com/hrygo/SpeechNexus/issues/17) | render_unit 依赖/局部重做、非空 source receipt、副本校验、独立质量/试听/匿名确认记录、有界完整导出 |
| D14 内容删除、保留与恢复 | A/B/C | [A07 #26](https://github.com/hrygo/SpeechNexus/issues/26)、[B01 #28](https://github.com/hrygo/SpeechNexus/issues/28)、[C00 #35](https://github.com/hrygo/SpeechNexus/issues/35)、[C07 #42](https://github.com/hrygo/SpeechNexus/issues/42)、[C08 #43](https://github.com/hrygo/SpeechNexus/issues/43) | [G1 #14](https://github.com/hrygo/SpeechNexus/issues/14)、[G2 #15](https://github.com/hrygo/SpeechNexus/issues/15)、[G3 #16](https://github.com/hrygo/SpeechNexus/issues/16)、[G4 #17](https://github.com/hrygo/SpeechNexus/issues/17)、[G5 #18](https://github.com/hrygo/SpeechNexus/issues/18) | 预检/明确确认、tombstone、正文清理、独立副本不误删、恢复先重放删除账本 |
| D15 浏览器体验、入口及内容网络 | A/B/C | [A07 #26](https://github.com/hrygo/SpeechNexus/issues/26)、[B07 #34](https://github.com/hrygo/SpeechNexus/issues/34)、[C04 #39](https://github.com/hrygo/SpeechNexus/issues/39)、[C06 #41](https://github.com/hrygo/SpeechNexus/issues/41)、[C08 #43](https://github.com/hrygo/SpeechNexus/issues/43) | [G2 #15](https://github.com/hrygo/SpeechNexus/issues/15)、[G4 #17](https://github.com/hrygo/SpeechNexus/issues/17)、[G5 #18](https://github.com/hrygo/SpeechNexus/issues/18) | HTTPS/WSS/Host/Origin、设备/播放/键盘/无障碍矩阵、无内容日志与出站实测 |
| D16 部署、观测、集群、恢复与发布 | A/B/C | [A07 #26](https://github.com/hrygo/SpeechNexus/issues/26)、[B02 #29](https://github.com/hrygo/SpeechNexus/issues/29)、[B07 #34](https://github.com/hrygo/SpeechNexus/issues/34)、[C08 #43](https://github.com/hrygo/SpeechNexus/issues/43) | [G3 #16](https://github.com/hrygo/SpeechNexus/issues/16)、[G5 #18](https://github.com/hrygo/SpeechNexus/issues/18) | 同构角色/唯一权威、目标物理拓扑、混合负载/长时/质量、告警、许可/运维和 RPO/RTO 演练 |

## 阶段跟踪清单

- [ ] #13 — [Gate G0] 接口、环境与关键风险探针验收
- [ ] #14 — [Gate G1] 标准文件语音与基础页面闭环验收
- [ ] #15 — [Gate G2] 实时语音、音色与实例生命周期验收
- [ ] #16 — [Gate G3] 完整语音底座与基础产品试点验收
- [ ] #17 — [Gate G4] 会议、提词、助手与有声书验收
- [ ] #18 — [Gate G5] 目标拓扑、恢复与完整交付资格验收

## 更新、验收与交接规则

1. 每 issue 只保留一个 status 标签：planned/ready/in-progress/integration/qualification/blocked/done；ready 由具名输入判断，blocked 写明原因、owner 和解除条件。
2. 工作包只有全部范围和真实验证结束才按 completed 关闭；PR 合并仅进入待联调/待资格，不能用关闭关键字把整个父包提前关闭。子任务 PR 引用父包并给对应检查项证据。
3. Gate 逐检查项核对当前集成 SHA、契约/模型/镜像/拓扑/语料、命令/标准/结果与未验证项。前置 Gate 未通过不跳关；总指挥根据三队联合结论放行。
4. 首个资源就接删除与恢复；未知执行保持占用，先有停止/释放证据再退预算。工作台仅走公开语音协议，LLM 默认关闭/批准内网，业务内容不出网。
5. 单 MegaCube 只支持其已测算力故障域，额外 GPU 节点接管/横扩资格另需资源，不能用单机逻辑集群或跨主机控制角色代替。
6. 每轮派工、集成和关口变更后刷新本矩阵；刷新脚本只读 GitHub、写本地文件，不会自动改标签、关闭 issue、同步总控正文或启动实施。

```bash
python3 scripts/dev-env/refresh-implementation-tracker.py
```

仓库编排材料：[整体计划](https://github.com/hrygo/SpeechNexus/blob/main/docs/plans/2026-10-23-three-team-delivery-plan.md)、[首轮指令](https://github.com/hrygo/SpeechNexus/blob/main/docs/plans/2026-10-23-agent-team-startup-briefs.md)、[编排清单](https://github.com/hrygo/SpeechNexus/blob/main/docs/plans/2026-10-23-implementation-issues.json)和[刷新脚本](https://github.com/hrygo/SpeechNexus/blob/main/scripts/dev-env/refresh-implementation-tracker.py)。三队从包含这些材料的同一集成版本启动；远端 issue 正文也提供自包含任务范围。后续刷新产生的总控正文由维护者核对后显式更新。

原规划代码事实基准 `390f121` 保留为历史时点；本次现实基线见页首，实施从用户指定集成 SHA 开始。创建 issue、契约或测试原型不代表业务已实现。
