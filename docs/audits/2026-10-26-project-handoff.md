---
title: "SpeechNexus 项目完整工作交接"
status: active
version: "1.1.0"
date: 2026-10-26
---

# SpeechNexus 项目完整工作交接

本文件是新负责人的统一接管入口，汇总 A/B/C 已集成内容、未完成范围、待审交付、
现有决定、证据与现场保留方式。后续优先级、负责人和实施窗口由新负责人重新安排；
历史计划中的“下一步”不作为自动开工指令，完整范围和原验收条件继续保留。

交接时，真实文件 ASR 和 CustomVoice 已有内部隔离执行结果，协调事务和工作台已有
分段实现；**公开上传→持久 Job/准入→真实执行→发布→SDK/页面下载的完整链仍未验收**。
25 个工作包完成 0/25，6 个 Gate 放行 0/6；这表示完整范围尚未通过，不表示没有实施。

## 1. 接管版本与待审交付

下表是 2026-10-26 17:51（Asia/Shanghai）全量 issue 核对时的快照。
接管时须重新读取远端；后续合并不追溯改变历史实验的受检版本。

| 项目 | 版本或入口 | 接管判断 |
| --- | --- | --- |
| 已集成 main | `54daef6ae4792b69a0e5e8645b09240ff715082e`，最后合并 #219 | 本文件所用产品基线 |
| B 收尾 PR | [#220](https://github.com/hrygo/SpeechNexus/pull/220)，本 PR | 文档、完整交接与 issue 核对；等待审阅，不自动合并 |
| A 收尾 PR | [#221](https://github.com/hrygo/SpeechNexus/pull/221)，head `b7e65f4a119999e720d594200c53dc683bec5526` | 已提交共享媒体包、私有音频检查/回执与窄消费者；尚未合入，B 尚未实际接收 |
| A 详细交接 | [#221 固定版本交接](https://github.com/hrygo/SpeechNexus/blob/b7e65f4a119999e720d594200c53dc683bec5526/docs/plans/2026-10-26-team-a-final-handoff.md) | 随候选评审，不能当作 main 文件或公开音频 ready |
| B 详细交接 | [团队 B 交接](2026-10-26-team-b-handoff.md) | B00–B07 剩余范围、真实探针、C 回执、归档与恢复 |
| C 当前事实 | [有声书实现/证据矩阵](2026-10-26-team-c-render-loop-status.md)、[证据保留事故](2026-10-26-team-c-evidence-retention.md)、#35–#43 | 分段实现和未决审查并存，不能只读成功摘要 |
| 调度与状态 | [总控 #6](https://github.com/hrygo/SpeechNexus/issues/6)、各 issue 正文 | 远端为当前进度来源，带日期矩阵仅为快照 |

#221 不是 #220 的已合入依赖；审阅 #220 时没有将 #221 的产品改动移入本 PR。
合并顺序由接管人决定，合入时分别验证组合版本并同步直接受影响的 issue。

## 2. 已接受的产品与架构决定

接管默认沿用下列决定；变更通过正式设计/契约过程，不因当前只有一个节点而简化语义。

| 边界 | 当前决定与权威入口 |
| --- | --- |
| 集群与数据库 | 必须支持集群；单节点使用同一持久协议。PostgreSQL 是唯一正式产品数据库，程序承担业务事务与编排，不恢复复杂存储过程业务引擎；InnoDB 仅作关键协调路径对照。[ADR 0004](../decisions/0004-cluster-ready-architecture-and-single-node-mvp.md)、[ADR 0006](../decisions/0006-cluster-coordination-and-storage-portability.md) |
| 协调选择 | 不采用 etcd；Redis 可作可丢失缓存/唤醒，不能成为唯一事实或第二预算权威。旧原型退出按 [ADR 0007](../decisions/0007-legacy-coordination-retirement.md)处理 |
| 生命周期 | 完整意图先持久化再外发；unknown 沿原身份恢复并继续占用。取消接受、断线、TTL 不代表停止或释放；双证据后由 A 原事务幂等结算。[事务方案](../architecture/coordination-transactions.md) |
| 模块与权限 | 工作台只用公开语音协议；网关管理执行屏障、引擎凭据与证据，控制器只管理批准模板。业务 API/worker 不持 Docker socket，不共写工作台/底座表。[所有权](../architecture/contract-ownership.md) |
| 契约与依赖 | Python/TS 边界类型同源生成或校验；领域向内，vendor/数据库/公开 DTO 在边界转换。[契约入口](../../contracts/README.md)、[目录边界](../architecture/repository-layout.md) |
| 产品身份 | 首期无登录、用户目录、RBAC、多租户或个人私有空间；两个 Web 入口不是隔离边界。[ADR 0001](../decisions/0001-product-boundaries-and-authentication-scope.md) |
| 内容网络 | 业务内容不出部署网络；LLM 默认关闭，只用服务端批准的本机/内网服务，不回退公有云。[ADR 0003](../decisions/0003-workbench-llm-network-boundary.md) |
| 制品与删除 | 私有 staging 后发布固定对象引用；独立保存副本不追删，删除账本重放完成后才能开放恢复内容。[数据删除](../architecture/data-deletion.md) |

## 3. 各团队已交付与仍缺的接线

| 团队 | main 已集成 | 剩余接线或资格 |
| --- | --- | --- |
| A | 同源契约/生成消费者、18 端口与 7 查询取证、协调事务/持久意图、启动身份守卫、配置/词典私有草稿、固定词典 GET、受限进程边界 | 正式音频 Asset ready、权威 binding/默认/资格、完整业务 Job/调度/准入、Artifact/Operation/公开发布、SDK 与页面同版闭环 |
| B | ASR #162/TTS #219 固定制品与注册输入；#168/#203/#209 真实默认文件执行；#178/#218 隔离整代次双事实→A 原租约结算；控制器/CPU VAD/区间组件 | 注册输入不等于 A 权威注册或发布；生产 stop 意图/准入仍有 fixture；单请求释放、TTS base、真实故障、完整参数/质量/实时/物理多机仍待 |
| C | 工作台持久版本与消费者、部分管理与语音页面、删除恢复；有声书公开派发/结算扫描、副本/历史页面、两条公开二进制下载 | 真正公开语音来源及助手成功链；导出 SSE 正式消费者；生成持久化/重放/CAS、分类/公平与页面恢复审查；物理设备、全状态无障碍和目标拓扑资格 |

A 的固定词典 GET #201 已合入 `064c6b6`，合前 CI40258176229 全 14 成功；
精确合后 CI40275218078 取消、清理核实未结，不能记为合后全绿。
#173 原精确合后 Web 焦点失败也继续保留；后续主干 CI 不覆盖其原失败事实。

## 4. 全部 40 个 issue 的去向

截至 2026-10-26 17:51:24（Asia/Shanghai）已完整回读全部 issue：
本轮更新 30 项，10 项已准确而未重复写入；均 open、每项一个有效状态标签。
34 项 `in-progress` 表示部分实施仍有实现工作，6 项 `planned` 表示尚未开始对应完整范围。
原目标、整个 AC 区域、负责人、里程碑和其他元数据逐项保留。

### 总控、Epic 与 Gate：13 项

| 层级 | issue | 当前状态 | 未完成判断 |
| --- | --- | --- | --- |
| 总控 | [#6](https://github.com/hrygo/SpeechNexus/issues/6) | in-progress | 全项目完整交付与 G0–G5 未通过 |
| S0 / G0 | [#7](https://github.com/hrygo/SpeechNexus/issues/7) / [#13](https://github.com/hrygo/SpeechNexus/issues/13) | 均 in-progress | 同版消费者/事务、全模型许可/探针、语料/浏览器矩阵与三队确认 |
| S1 / G1 | [#8](https://github.com/hrygo/SpeechNexus/issues/8) / [#14](https://github.com/hrygo/SpeechNexus/issues/14) | 均 in-progress | 两条真实文件公开闭环及其完整故障矩阵 |
| S2 / G2 | [#9](https://github.com/hrygo/SpeechNexus/issues/9) / [#15](https://github.com/hrygo/SpeechNexus/issues/15) | 均 in-progress | 真实双工、声音复用/发布与完整生命周期 |
| S3 / G3 | [#10](https://github.com/hrygo/SpeechNexus/issues/10) / [#16](https://github.com/hrygo/SpeechNexus/issues/16) | 均 in-progress | 完整底座、共享双库行为、真实消费者与混合负载 |
| S4 / G4 | [#11](https://github.com/hrygo/SpeechNexus/issues/11) / [#17](https://github.com/hrygo/SpeechNexus/issues/17) | 均 in-progress | 会议/助手/有声书真实闭环及恢复 |
| S5 / G5 | [#12](https://github.com/hrygo/SpeechNexus/issues/12) / [#18](https://github.com/hrygo/SpeechNexus/issues/18) | S5 in-progress；G5 planned | 物理多机、完整备份恢复、长时质量与发布交接 |

Epic、工作包全范围完成与 Gate 分阶段放行分别判断；原有前置和用户放行要求保持。

### 工作包：25 项

| 工作包 | issue | 当前状态 | 主要剩余范围 |
| --- | --- | --- | --- |
| A00 契约交接 | [#19](https://github.com/hrygo/SpeechNexus/issues/19) | in-progress | 其余音频/配置差异、跨字段/时序、公开消费者联验；#221 待审 |
| A01 协调事务 | [#20](https://github.com/hrygo/SpeechNexus/issues/20) | in-progress | 真实策略/消费者、完整权限竞争恢复与双库联合证据 |
| A02 资产与发布 | [#21](https://github.com/hrygo/SpeechNexus/issues/21) | in-progress | #221 集成/B 接收、公开音频 ready、权威发布/CAS 与制品引用 |
| A03 调度准入 | [#22](https://github.com/hrygo/SpeechNexus/issues/22) | in-progress | 业务 worker/公平队列、完整配额/父预算与正式恢复结算 |
| A04 REST/SSE/MCP | [#23](https://github.com/hrygo/SpeechNexus/issues/23) | in-progress | 文件 Job/Operation/事件、管理写入、公开闭环和 MCP |
| A05 公开实时 | [#24](https://github.com/hrygo/SpeechNexus/issues/24) | planned | 会话/epoch、水位/credit、双工准入及恢复 |
| A06 声音发布 | [#25](https://github.com/hrygo/SpeechNexus/issues/25) | planned | 设计/克隆、候选复用、不可变发布与生命周期 |
| A07 恢复与发布 | [#26](https://github.com/hrygo/SpeechNexus/issues/26) | in-progress | 音频/跨模块删除、备份恢复水位、迁移回退和运维许可 |
| B00 环境/模型 | [#27](https://github.com/hrygo/SpeechNexus/issues/27) | in-progress | 全模型/许可 manifest、权威 binding 与逐能力资格 |
| B01 执行网关 | [#28](https://github.com/hrygo/SpeechNexus/issues/28) | in-progress | 生产 submit/observe/abort、真实故障与单请求释放 |
| B02 控制器 | [#29](https://github.com/hrygo/SpeechNexus/issues/29) | in-progress | 持续 readiness、重启/多代竞争、peak/steady/drain |
| B03 文件执行 | [#30](https://github.com/hrygo/SpeechNexus/issues/30) | in-progress | 两条生产公开链、完整参数/长文本/Base 和同版资格 |
| B04 流式执行 | [#31](https://github.com/hrygo/SpeechNexus/issues/31) | in-progress | 真实 PCM/WS、背压、流式及双工配方 |
| B05 声音材料 | [#32](https://github.com/hrygo/SpeechNexus/issues/32) | planned | VoiceDesign/Base、克隆、复用与材料恢复 |
| B06 辅助语音 | [#33](https://github.com/hrygo/SpeechNexus/issues/33) | in-progress | 实时 VAD、对齐/匿名分人与逐绑定质量 |
| B07 部署资格 | [#34](https://github.com/hrygo/SpeechNexus/issues/34) | planned | 质量容量、混合/长时/故障与物理多机 |
| C00 版本/客户端 | [#35](https://github.com/hrygo/SpeechNexus/issues/35) | in-progress | 事件/实时消费者、覆盖识别/摘要、证据重建与恢复水位 |
| C01 管理/任务 | [#36](https://github.com/hrygo/SpeechNexus/issues/36) | in-progress | 实际资格/预算/策略、管理写入、分页/重试与联合验收 |
| C02 文件页面 | [#37](https://github.com/hrygo/SpeechNexus/issues/37) | in-progress | 真实 ASR/TTS、字幕/Range/格式、取消未知与长时恢复 |
| C03 声音工作室 | [#38](https://github.com/hrygo/SpeechNexus/issues/38) | in-progress | 真实材料/设计/克隆、复用资格、发布和生命周期 |
| C04 浏览器媒体 | [#39](https://github.com/hrygo/SpeechNexus/issues/39) | in-progress | 公开双工/背压、真实设备/网络/跨浏览器及完整无障碍 |
| C05 会议/提词 | [#40](https://github.com/hrygo/SpeechNexus/issues/40) | in-progress | speech session、分人对齐、固定导出和真实跟随 |
| C06 LLM/助手 | [#41](https://github.com/hrygo/SpeechNexus/issues/41) | in-progress | 真实 Job/已发布声音、插话/停止、独立保存与网络质量 |
| C07 有声书 | [#42](https://github.com/hrygo/SpeechNexus/issues/42) | in-progress | 事务/重放/CAS、分类公平、真实生产者、检查点/续作及质量 |
| C08 应用资格 | [#43](https://github.com/hrygo/SpeechNexus/issues/43) | in-progress | 证据重建、设备/浏览器/辅助技术、恢复水位和目标部署 |

### 阶段与依赖交接：2 项

| issue | 当前状态 | 接收内容与未完成边界 |
| --- | --- | --- |
| [#142 B-G1](https://github.com/hrygo/SpeechNexus/issues/142) | in-progress | 首条文件 ASR/CustomVoice 的持久执行与停止事实；A 生产接入、公开发布/消费和完整故障仍待 |
| [#149 A→C06](https://github.com/hrygo/SpeechNexus/issues/149) | planned | 可查询的真实 Speech Job、已发布声音及助手成功链；不前移为文件 G1 放行依赖 |

上述表是导航，不替代 issue 的完整任务、AC、依赖和版本证据。规划刷新器只覆盖原 38 项，
本次另外逐项核对 #142/#149，不能以“38 项刷新成功”代替全量 40 项核对。

## 5. 接管时须带走的风险与待决事项

这些事项保留原范围；表中责任边界不是给新负责人新增排期。

| 事项 | 已知事实、风险与解除条件 | 接收边界 |
| --- | --- | --- |
| 文件公开链 | B 内部真实执行/注册输入已交，A 权威配置、音频 ready、业务 Job/准入与发布仍缺；同版 SDK/C 消费后再验 G1 | A/B/C，#14/#21/#22/#23/#142 |
| 共享媒体包 | #221 已固定提交私有成功回执/decoder/窄消费者，默认仍 TextAssetInspector；需评审合入、B 实际兼容接收及生产装配，不能仍称“尚未提交” | A/B，#19/#21 |
| TTS 参数兼容 | Nexus overrides 2000、标准 speech/内部命令 4096、固定 pipeline 默认声明 500；实际 adapter 校验、2001–4096 准入和 token 总预算未验证，禁止静默截断 | A/B/C，#30/#37/#142 |
| 请求级音频上限 | #203 未把更紧的请求时长传入执行 policy；管理方合成反例确认请求 100 ms 仍使用 600000 ms / 8 MiB binding policy，并返回 200 ms。#219 未修复，须收紧时长/PCM预算并覆盖拒绝与资源边界；见[B 详细交接](2026-10-26-team-b-handoff.md) | B03，#30/#142；与指令字符上限分别处理 |
| 停止与结算 | 隔离整代次停止成立；单请求释放未获资格，TTS 只结算执行租约、16 GiB base 未结算，不能迁移 ASR base 结论 | A/B，#22/#28/#29/#142 |
| 有声书外发/恢复 | #197/#198 等已合入；先外发后落库、同键异体/可变快照重放、锁前读与 CAS/source 一致性审查未结，默认结算循环关闭 | C/A 公开边界，#42 |
| 副本/分页/页面 | 来源错误类型与捕获分类不一致、最老批次饥饿、无持久退避；SQL 全读后切片、仅 useRef 键跨刷新丢失、请求乱序/未知误报及焦点审查未结。#216/#217 仅 fake 参数转发 | C，#42/#43 |
| C 验收证据 | #189 清理丢失 82 份原件；278 条中 214 条引用受影响，全部 278 条仍 not_run，需在稳定根重建并核对源码/哈希；不能从历史摘要补勾 | C，#35/#43 |
| C 操作覆盖 | 机器清单 52 操作、42 bound、10 consumer_pending；旧 Markdown 51/41 待同步，扫描范围/未决审查待核。bound 只表示该清单映射状态 | C，#35 |
| 浏览器/LLM | 入口页 52 项 a11y 复跑摘要仍须核对精确构建与原件；不代表全状态读屏/设备。3 项真实 LLM 资格未执行，助手依赖 #149 | C，#39/#41/#43 |
| CI 五分钟目标 | #219 原 PR Python job 8 分 46 秒，完整 CI 尚未达到目标；文档轻量成功不证明全量提速，不能以删测试/降覆盖或省略双库达标 | 接管人指定，CI 规则 |
| 目标环境 | 集群是硬要求；物理多机、网络分区、备用算力、备份恢复、长时质量/容量和完整许可运维交接仍未通过 | #18/#26/#34/#43 |

C 对 B 审查意见的回执及未集成本机 v1.1.0 计划见
[B 交接](2026-10-26-team-b-handoff.md)和
[共享审查评论](https://github.com/hrygo/SpeechNexus/issues/142#issuecomment-6411923009)；
回执不等于问题修复或联合验收。#215 只是缺口登记，不能记为上述问题已经修复；
项目已有隔离 MinIO 测试能力，不能把全部 C 自有问题归为外部未提供环境。

## 6. 证据、草稿和本机现场

B 私有归档的数量、哈希校验和清理是原执行方报告；管理方本次只核对已提交报告和公共 CI，尚未读取归档字节，接收方待核验。表中“核验”须按该来源理解；C 的本机稳定归档与 A 的私有原件分别接收，不相互替代。

| 内容 | 位置或索引 | 使用与保留要求 |
| --- | --- | --- |
| 已入库设计/契约 | [文档索引](../README.md)、[技术方案](../architecture/technical-blueprint.md)、[contracts](../../contracts/README.md)、accepted ADR | 设计预期与实测分开；冲突保留并修正，不能放宽门禁 |
| B 实验与制品 | [B 交接证据表](2026-10-26-team-b-handoff.md)、ASR/TTS 固定注册输入及原报告 | 保留原模型/镜像/候选/fixture/拓扑；多个局部版本不能拼成同版生产资格 |
| B 私有归档根 | 本机 `.runtime/probes/team-b-closeout/archive-root.txt` 指向仓库外稳定目录 | 仅部署内访问，可能包含私有配置，不上传 Git/CI；换机先核验并移交访问 |
| 旧 skill 草稿/历史 | 同根 `skills-history.bundle`、`skills-worktree-wip.tar.gz`、`skills-worktree-manifest.json`、`archive-verification.json` | 10 份未提交文件已逐份核验，恢复到新目录；未集成，不替代仓库现有技能 |
| 私有实验原件 | 同根 `runtime-probes.tar.gz`，原 `.runtime/probes/` 仍保留 | 归档时 5308 文件、512351757 字节，完整读取验证；不删除或写回运行配置 |
| 全量 issue 核对 | 同根 `all-issue-audit-final-report.json`、`all-issue-audit-report.md`、`all-issue-audit-20261026T175124+0800.tar.gz` 及同名 verification JSON | 40 项快照、前后正文/摘要、并行更新保留和逐项回读；70 个归档文件摘要全部核验 |
| C 原件缺失说明 | [证据事故](2026-10-26-team-c-evidence-retention.md) | 82 份不可恢复，用新证据重建；不把复跑当原件找回 |
| 真实语料 | 根 [AGENTS.md](../../AGENTS.md) 的语料索引及主机本地目录 | 原始/派生/标注混合，文件数不等于独立样本；私有语料不入库 |

B 清理时未发现遗留 ASR/TTS 实验容器，35B、embedding、reranker 的容器身份和
启动时间前后相同；未停止或重启它们。旧 B skill 工作树/本地分支已在归档后删除，
当前交付分支仅用于 #220。这些是收尾时点事实，不保证接管时服务仍健康。

恢复旧 skill 的具体命令与先验校验见 [B 交接](2026-10-26-team-b-handoff.md)。
恢复前先核验归档摘要；独立新目录恢复，不覆盖当前源码、数据库、对象区或运行凭据。
`.venv/`、`node_modules/` 可按锁文件重建，不是唯一业务证据；原件不随 worktree 清理。

## 7. 接管与验证方式

1. 读取本文件、#6 和目标 issue，确认远端 main 与 #220/#221 当前状态。
   获取明确集成版本后再判断能力，不从旧计划日期或绿色 CI 推断验收完成。
2. 取得部署内归档访问并核对 manifest，确认私有证据、语料和受保护服务仍可定位。
   项目默认隔离端口和工具前置见[开发环境](../development-environment.md)；
   健康/就绪文档不是当前运行事实。
3. 由新负责人指定目标切片、owner、版本和环境；先冻结实际变化的生产者/消费者契约，
   再按现有[项目技能入口](../agent-guide.md)实施，保留上述未完成范围。
4. 按[验证指南](../agent-verification.md)选择检查。`make verify`、集成目标和部署启动
   会管理隔离服务，不作为阅读交接时自动执行的命令；不得连接同机共享实例。
5. 收集同版真实链及其失败/恢复证据后，再按 issue 原 AC 更新状态；
   Gate 保留前置、三队确认和用户放行。合并后使用仓库现有 issue 同步技能。

交接阅读可先运行下列只读检查，不会启动模型或数据库：

```bash
git status --short
git rev-parse HEAD
git worktree list
gh pr view 220 --json state,headRefOid,baseRefName,url
gh pr view 221 --json state,headRefOid,baseRefName,url
gh issue view 6 --json state,labels,body,url
```

## 8. 本次交付的验收边界

本 PR 修改文档，检查本地链接、命令语法、事实来源、40 项 issue 回读及 diff；
精确 head 的检查以 PR CI 为准。[CI 规则](../architecture/ci-validation.md)
允许纯 Markdown 路由仅执行适用检查，业务/数据库/部署不适用不表示重跑通过。

不重新执行历史业务、数据库、模型、质量或物理拓扑实验；历史结果继续绑定原报告。
本文件交付完整接管信息，不关闭工作包、阶段 Goal 或 Gate，也不自动开始新一轮研发。
