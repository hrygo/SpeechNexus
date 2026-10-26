# SpeechNexus 实施计划

- [2026-10-26 团队 A 最终交接](2026-10-26-team-a-final-handoff.md)：本轮共享媒体/私有回执交付、验证、未接线能力及待决事项；原排期终止，后续交新负责人重新安排。
- [2026-10-26 团队 A 进展复盘与原下一阶段计划](2026-10-26-team-a-review-and-next-phase.md)：历史基线、KR 与故障矩阵，已由收尾交接取代当前派工。

- [2026-10-24 团队 C Meeting/Minutes HTTP](2026-10-24-team-c-meeting-http-handoff.md)：五项持久业务操作、原子封存/纪要关联、原键恢复及会议删除依赖；默认工厂13项，显式装配18项，生产/会议页面及完整资格继续。

计划按基准日期保存，阶段完成状态须有实际交付和验收证据。

- [2026-10-24 团队 C 7cd18879集成](2026-10-24-team-c-7cd-integration-handoff.md)：安全吸收#77/#79/#80/#81，保留237文件并重生成交集，新增Meeting/Minutes与公开realtime解析依赖；Python2735/PG269/Web1150及Chromium合成流程通过，实际handlers/会议与实时业务继续。
- [2026-10-24 团队 C 项目与稿件版本工作台](2026-10-24-team-c-c1-editor-handoff.md)：正式生成客户端/页面、草稿与原键恢复、历史/冲突校对；53项针对性、Web940及4个Chromium合成上下文，一次终审两项Important已TDD修复，生产装配与完整资格继续。步骤见[实施计划](2026-10-24-team-c-c1-editor-plan.md)。
- [2026-10-24 团队 C C1列表、状态与删除HTTP](2026-10-24-team-c-c1-management-handoff.md)：3b8052安全集成、完整13项C1操作、作用域游标与版本化删除；Python2414/PG269/Web891，生产装配及完整资格待接入。步骤见[实施计划](2026-10-24-team-c-c1-management-plan.md)。

- [整体实施总控 #6](https://github.com/hrygo/SpeechNexus/issues/6)：GitHub 原生 Epic/工作包/Gate 层级与整体派工入口。
- [2026-10-23 实施跟踪矩阵](2026-10-23-implementation-tracking.md)：原规划 38 个 issue，另列 #142/#149 阶段交接、具名制品依赖、联合验收与分段证据；由[编排清单](2026-10-23-implementation-issues.json)和只读刷新脚本生成状态快照。
- [2026-10-23 三团队整体实施计划](2026-10-23-three-team-delivery-plan.md)：agent 整体实施调度入口；三环境责任、25 个工作包、开工/联调依赖、六波次关口与同步规则。
- [2026-10-23 agent 三队启动指令](2026-10-23-agent-team-startup-briefs.md)：可直接下发至云环境、MegaCube、MacBook 的首轮任务与统一交接模板。
- [2026-10-23 团队 A 执行目标](2026-10-23-team-a-goal.md)：A00–A07 的历史目标、KR 和完整范围；已转交新负责人重新安排，不再作为当前排期。
- [2026-10-23 团队 B 执行目标](2026-10-23-team-b-goal.md)：B00–B07 的持续目标、MegaCube 环境与停止证据探针、网关/控制器首个单元与阻塞条件；应用 Goal 是否启用单独核验。
- [2026-10-24 团队 C 执行目标](2026-10-24-team-c-goal.md)：C00–C08 的持续范围、首轮接力、模块所有权与完成条件；[C00 版本单元交接](2026-10-24-team-c-c00-version-handoff.md)、[C00 生成请求消费交接](2026-10-24-team-c-c00-project-boundary-handoff.md)、[C00 元数据分页交接](2026-10-24-team-c-c00-listing-handoff.md)、[C00 PostgreSQL 事务交接](2026-10-24-team-c-c00-postgres-handoff.md)、[C00 删除事务交接](2026-10-24-team-c-c00-deletion-handoff.md)、[C01 Admin 任务中心交接](2026-10-24-team-c-c01-task-center-handoff.md)、[C02 文件任务与 A00 消费交接](2026-10-24-team-c-c02-file-flow-handoff.md)、[C02 原始转写导入交接](2026-10-24-team-c-c02-transcript-import-handoff.md)、[C04 合成播放交接](2026-10-24-team-c-c04-media-handoff.md)、[C04 浏览器采集交接](2026-10-24-team-c-c04-capture-handoff.md)记录本地实现与未验证项。
- [2026-10-24 原命令与对象输入接入](2026-10-24-b03-frozen-inputs.md)：B01/B03 原持久命令、固定版本 MinIO 和现有文件消费者的输入接入边界。
- [2026-10-25 持久文件执行接线](2026-10-25-b03-durable-file-runner.md)：提交后启动、原身份恢复、私有回执及后台生命周期；不替代真实引擎与公开业务链资格。
- [2026-10-22 分阶段实施计划](2026-10-22-speechnexus-implementation-plan.md)：P0–P5、工作包、契约产物与初始预算。

- [2026-10-24 团队 C C00 文本字符边界交接](2026-10-24-team-c-c00-text-handoff.md)：统一拒绝不可存储字符、保留合法文本，记录失败回滚与同键重试证据。
- [2026-10-24 团队 C C02 文件任务跨刷新恢复交接](2026-10-24-team-c-c02-recovery-handoff.md)：记录创建/取消原键、只读恢复、事件水位及 Chromium 刷新证据；生产入口仍待接入，上传阶段恢复见后续交接。
- [2026-10-24 团队 C C02 文本配音交接](2026-10-24-team-c-c02-tts-handoff.md)：固定文本素材及声音/参数版本，记录合成音频试听下载与 TTS 刷新证据。
- [2026-10-24 团队 C C02 上传阶段刷新恢复交接](2026-10-24-team-c-c02-upload-recovery-handoff.md)：原输入与原上传键恢复、素材确认到 Job 的单记录交接，记录合成 HTTP/浏览器证据。
- [2026-10-24 团队 C C02 固定版本文本导出交接](2026-10-24-team-c-c02-text-export-handoff.md)：固定原文/校对 revision 的 UTF-8 输出、事务外编码、删除后拒绝读取及 A00/C02 接入清单。
- [2026-10-24 团队 C C02 文本下载契约候选](2026-10-24-team-c-c02-download-contract-handoff.md)：显式 revision、完整 UTF-8/空稿、响应头/错误和正负验收；待 A/C 正式冻结。
- [2026-10-24 团队 C C01 任务事件与正式入口](2026-10-24-team-c-c01-events-handoff.md)：同源公开 Job/SSE、已确认水位/历史过期、正式 Admin 宿主及生产 bundle 的合成 HTTP 验证。
- [2026-10-24 团队 C C01 Admin 同标签页恢复](2026-10-24-team-c-c01-recovery-handoff.md)：取消原键/目标、水位跨刷新与存储故障阻断，记录生产 bundle 的 Chromium 合成 HTTP 证据。

- [2026-10-24 团队 C C02 素材 HTTP 原身份恢复](2026-10-24-team-c-c02-asset-reconcile-handoff.md)：A uploadAsset/Location 消费、原 Asset hint 跨刷新、无替代上传与合成 Chromium 证据。

- [2026-10-24 团队 C C02 正式文件转写入口](2026-10-24-team-c-c02-host-handoff.md)：真实文件与同源公开 API、跨刷新原状态/事件/取消、原始 JSON 下载及正式 Chromium 页面证据。

- [2026-10-23 架构与契约修复验收](2026-10-23-architecture-contract-repairs.md)：已完成的逐项交付与闭环证据。
- [2026-10-23 集群协调重构计划](2026-10-23-cluster-coordination-refactor.md)：精简 DDL、事务契约和双库测试原型已交付；记录生产接入、生命周期与集群故障资格的剩余验收。

[项目主设计](../speechnexus-product-architecture.md) · [文档索引](../README.md)

- [2026-10-24 团队 C C02 正式文本配音入口](2026-10-24-team-c-c02-tts-host-handoff.md)：固定模型/声音/参数版本、原文本与原 Asset 恢复、WAV 试听下载与切换释放的正式 Chromium 证据。

- [2026-10-24 团队 C C05 固定稿件封存](2026-10-24-team-c-c05-seal-handoff.md)：追加不可变 sealed revision、精确历史源、owner-only v2 迁移及真实 PG 重放/删除证据；会议页面和公开接口待接入。

- [2026-10-24 团队 C C05 提词跟随与手动阅读](2026-10-24-team-c-c05-prompter-handoff.md)：有限窗口/旧稿旧epoch隔离核心、正式手动阅读、显式浏览器保存与恢复；自动语音接线及服务端持久化待完成。

- [2026-10-24 团队 C C05 服务端提词持久化](2026-10-24-team-c-teleprompter-persistence-handoff.md)：接入两项已声明提词操作的独立 PG 事务、旧稿 revision 拒绝与原键重放、tombstone/cleanup 级联与最小权限；实时接线和真实语音跟随待完成。

- [2026-10-24 团队 C a98b2bb 集成核验](2026-10-24-team-c-a98-integration-handoff.md)：保留173文件改动并吸收上游 #65–#68，记录组合验证及明确生成入口依赖。

- [2026-10-24 团队 C C06 内网 LLM 与纪要候选](2026-10-24-team-c-c06-llm-handoff.md)：默认关闭、固定调用/预算/取消、sealed 源重检与真实 PG 删除屏障；持久任务、公开页面、助手和真实部署资格待完成。

- [2026-10-24 团队 C C06 持久纪要任务](2026-10-24-team-c-c06-tasks-handoff.md)：原意图任务/attempt、绑定固定、提交未知恢复、最终发布屏障及未知请求物理清理保护；公开接入和真实资格待完成。

- [2026-10-24 团队 C 2874c839 集成核验](2026-10-24-team-c-2874-integration-handoff.md)：保留191文件并吸收#69–#71；原样1659测试、隔离PG探针262、Web530和四条正式Chromium合成流程通过，公开生成入口和真实资格仍待完成。

- [2026-10-24 团队 C C07 固定依赖与局部失效](2026-10-24-team-c-c07-render-handoff.md)：不可变版本指纹、显式生成代次、改文/角色换声局部stale核心；真实持久化/保存副本/独立导出及页面资格待完成。步骤见[实施计划](2026-10-24-team-c-c07-render-plan.md)。

- [2026-10-24 团队 C C1 JSON消费者与7372778集成](2026-10-24-team-c-c00-c1-handoff.md)：保留198文件、吸收#72–#74，接稿件命令及项目/稿件生成响应，记录1902测试/PG262/Web584/四条浏览器合成证据；HTTP和完整业务资格待完成。步骤见[实施计划](2026-10-24-team-c-c00-c1-plan.md)。

- [2026-10-24 团队 C C1 HTTP与e0d8992集成](2026-10-24-team-c-c00-http-handoff.md)：保留204旧C文件及双方README，消费生成HTTP入口和补充错误契约，七个项目/稿件操作与一致历史快照；2205测试/PG265（含真实loopback）/Web815，终审默认docs出站问题已TDD修复，生产root/迁移、页面和完整真实资格待完成。步骤见[实施计划](2026-10-24-team-c-c00-http-plan.md)。

- [2026-10-24 团队 C 会议记录与纪要工作台](2026-10-24-team-c-meeting-ui-handoff.md)：批准模板发现/源CAS、正式页面与标签页恢复；生产 Chromium→真实 C HTTP→隔离PG/fake LLM证据，真实模型/设备与生产装配仍待完成。步骤见[实施计划](2026-10-24-team-c-meeting-ui-plan.md)。

- [2026-10-24 团队 C 9eea859 依赖集成](2026-10-24-team-c-9ee-integration-handoff.md)：保留266文件，吸收#82/#83/#84；264个文件字节不变、README双语合并，Python3006/Web1268/PG295和C生产bundle真实浏览器回归。专业运行资格仍分别验收。
- [2026-10-24 团队 C 删除预检、确认与恢复页面](2026-10-24-team-c-deletion-ui-handoff.md)：正式页面、原键恢复和完整范围确认；Web1283、隔离PG296及生产bundle真实HTTP浏览器。saved_asset完整图与A07恢复资格仍待联验。
- [2026-10-24 团队 C 实时ASR消费与字幕页面](2026-10-24-team-c-realtime-asr-handoff.md)：生成事件语义、有界credit/PCM、原键恢复、最终正文草稿、断线gap及正式页面；生产bundle的合成REST/WS/麦克风验证，生产实时/TTS/双工/真实资格继续。[实施计划](2026-10-24-team-c-realtime-asr-plan.md)。

- [2026-10-24 团队 C 实时TTS消费与播放](2026-10-24-team-c-realtime-tts-handoff.md)：固定引用、文本冻结/credit、原键恢复、PCM有界Worklet与独立生成/网络/渲染水位；合成真实浏览器证据，生产TTS/双工与真实资格待验。[实施计划](2026-10-24-team-c-realtime-tts-plan.md)。

- [2026-10-24 团队 C eb299f7 依赖集成](2026-10-24-team-c-eb299-integration-handoff.md)：保留309文件并吸收#85–#88；Python3314/Web1471/隔离PG及业务浏览器296，ASR/TTS合成浏览器回归通过，专业运行资格及完整工作包继续验收。

- [2026-10-24 团队 C 音色设计/克隆创建任务](2026-10-24-team-c-voice-create-handoff.md)：正式输入、已有voice_reference材料确认、持久Job及原键/存储屏障；候选/校验/发布/复用与真实资格继续。[实施计划](2026-10-24-team-c-voice-create-plan.md)。

- [2026-10-24 团队 C 工作空间读取与2a46依赖集成](2026-10-24-team-c-workspaces-read-handoff.md)：生成GET列表/详情、分页/历史/迟到隔离、正式Admin与合成浏览器，保留生产资格余项。

- [2026-10-24 团队 C Workspace 实际 HTTP / PG 浏览器联验](2026-10-24-team-c-workspaces-live-handoff.md)：吸收 #94，生产 Admin 经真实 A GET/SQL 读取隔离 PG；显式服务注入，不宣称完整 AssetRuntime/S3 资格。

- [2026-10-24 团队 C 独立工作台服务入口](2026-10-24-team-c-runtime-root-handoff.md)：真实 PG 装配、只读启动检查、非 owner 权限与默认关闭的 LLM；fresh owner baseline 由 A 协调迁移，完整生产资格继续。
