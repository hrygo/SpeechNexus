---
title: "SpeechNexus 资源治理与持久化设计"
status: active
version: "0.5.7"
date: 2026-10-23
---

# SpeechNexus 资源治理与持久化设计

本文负责容量准入、调度、取消 / 接管、用量、持久任务与数据生命周期，属于[项目主设计](../speechnexus-product-architecture.md)的配套规范。首期按部署 / 工作空间 / 执行类别治理，不建设用户或租户权限。硬件配置由部署资格确定；以下规则是待实现的不变量，不能代替实际容量、质量与恢复验证。

[ADR 0006](../decisions/0006-cluster-coordination-and-storage-portability.md)已将跨表
规则的主要执行边界调整为受控应用事务。PostgreSQL 是唯一正式交付数据库，
MySQL/InnoDB 仅用于关键协调路径验证；不采用 etcd，Redis 不承担协调权威。
活动 DDL 已精简，规则的程序实现见[受控事务方案与契约](coordination-transactions.md)；
测试/工具原型在真实 PostgreSQL 与 MySQL 上验证关键路径。生产调度、运行时接入与完整
集群故障资格仍按[重构计划](../plans/2026-10-23-cluster-coordination-refactor.md)完成。

## 1. 硬件无关的资源准入与调度

### 1.1 逻辑资源池与容量声明

资源池按能力与服务类别组织，例如 `asr-realtime`、`tts-realtime`、`asr-batch`、`tts-batch`、`voice-creation`、`auxiliary`；它们是逻辑隔离边界，部署时可以映射到同机或不同节点。物理与虚拟算力节点以 `HostNode`（`host_node_id`）建模，映射到同一 `host_node_id` 物理设备的池与运行实例必须共享该节点的父级物理预算，不能各自把整卡或整机统一内存容量算作可用；不同 `host_node_id` 节点的父物理预算彼此独立，单机 MVP 部署对应仅注册一个节点的同构特例（见 [ADR 0004](../decisions/0004-cluster-ready-architecture-and-single-node-mvp.md)）。

已有 vLLM 接入分为 managed 和 external：managed 要求平台能够控制入口、统计整个池预算并执行取消 / 封锁屏障，是首版生产资源治理的默认前提；external 仅能限制 SpeechNexus 自己的在途请求，不能证明服务中其他调用方的占用。没有提供隔离容量与取消证据的 external 绑定只能发布为 best_effort / experimental，不能沿用 managed 的实时 SLO、全局容量或故障接管承诺。

每个 `ModelBindingRevision` 关联一份 `CapacityManifest`：支持任务与格式、模型 / 引擎 revision、部署拓扑引用、常驻预算、每 active slot 的增量资源上界、最大 active slots、预填充与输出速率限制、最大段预算、取消方式、最坏非抢占执行时间、资格报告与有效期。每个 stage 显式声明 `emits_audio_pcm`；只有该类 stage 才能携带 `max_output_audio_ms`，提交同时必须给出 16k / 24k / 48k 之一的 `sample_rate_hz`，网关按 `floor(max_output_audio_ms * sample_rate_hz / 1000)` 冻结 hard output ceiling。绑定的 `(binding_id, revision)` 由注册表分配不可变且全局唯一的 `revision_id`；容量、资格、实例、租约、submit intent 与 receipt 都引用同一值，不各自推导字符串。

资格报告按 `capacity_manifest_id` 为执行图中的每个 `stage_id` 记录实际的 engine kind、镜像摘要与 runtime 版本；单镜像字段不能代表包含 VAD、ASR 复核或 TTS 多 stage 的完整配方。草稿可以声明目标 `production` tier，但只有发布、退役和紧急禁用状态必须绑定已通过的必要场景与完整 stage 软件身份。`production` 资格至少包含一个 required scenario，并须全部 passed、整体 pass 且设置到期时间；managed 容量必须声明 fencing barrier，engine abort 模式必须声明真实 request abort，production 还必须声明硬输出上限。

资格报告的计算口径是封闭的：`test_suite.scenario_ids` 与 `scenarios[].scenario_id` 必须一一对应且无重复（生产者校验）；`required_scenarios_passed` 仅在至少一个 required scenario 存在且全部 passed 时为 true；`overall_status` 在任何 required scenario failed 时为 `fail`，在 required 全部 passed 时为 `pass`，否则为 `incomplete`。optional scenario 是非门禁证据，非 passed 时必须写入 `limitations`，不能据此宣称生产质量。发布流程还必须解析报告引用的 binding 与 capacity manifest：`binding` 的 `id / revision`、`binding_revision_id`、`capacity_manifest_id` 和 `release_tier` 必须一致；`software.engine_components` 必须与 manifest 的每个 `stage_id` 一一对应且 engine kind 相同；报告拓扑的 memory architecture 必须与 manifest `topology_ref` 相同；`qualified_at` 必须早于 `expires_at`。这些跨文档规则由生产者和契约语义测试执行。

资源向量至少包含 engine active slots、设备内存、CPU 解码额度、主机 PCM buffer、输入 / 输出带宽和对象存储额度。常驻模型内存在引擎级只计一次，不为每个请求重复收费；请求只申请增量预算。多阶段 TTS 的 Talker、Code2Wav 及 connector 预算作为完整执行图申报。

完整执行图也覆盖实时 ASR 的定稿依赖。启用 `full_segment` 的配方必须把 1.7B 复核的就绪、保留预算和等待期限纳入实时资格，required 对齐 / 分人亦同；不能仅按 0.6B 预览占用宣称可准入。文件任务即使复用该实例，也只能使用不破坏实时承诺的预算。绑定 revision / 资格、具体实例及 generation、准入租约各自独立记录，空闲卸载不退役绑定。

容量未知、物理父预算缺失或资格不匹配时，该绑定不接生产实时负载；受控实验可以显式使用单槽，但不能因此宣称生产并发。更换引擎、精度、模型、重要 stage 配置或驱动组合会使对应容量资格失效，需重新验证。公共 API 只暴露逻辑能力与可用性，不写死 GPU 名称或数量。

### 1.2 三层准入

```text
部署入口与资源 / 功能状态检查
  → 参数 / 版本 / 能力验证
  → 部署、工作空间、请求类别的请求与累计额度
  → 队列期限与长度限制
  → 父物理预算 + 引擎 / stage 资源原子预留
  → adapter 提交执行
  → 确认完成 / 取消
  → 结果发布与用量结算
  → 资源释放
```

配额包括活跃实时会话、在途同步请求、后台并发、排队数量、每日输入音频时长、TTS 字符 / 输出时长、存储与导出速率。请求中的 max_queue_wait_ms 只能收紧管理策略；普通推理参数不能增大这些配额。

实时累计额度与缓冲 credit 分开管理。每次发放音频 / 文本 credit 前，以原子事务在部署、工作空间和执行类别的适用层级预留相应累计输入额度。ASR 在样本验证并 accepted 时将该区间从预留转为累计使用；缓冲消费、预览结束、clear 或取消不退还已接受音频额度。TTS append 被 accepted 时继续保留文本额度，只在首次冻结该文本区间时转累计使用；取消 / 关闭释放尚未冻结的尾部和未使用 credit，冻结部分不退还。ack 后再次发放缓冲 credit 时仍须核对累计账本，不能凭释放内存重复透支。长连接拿不到新额度时停止发 credit，发送 quota_exceeded 与剩余已接受工作的处理方式；不接受超额输入，有限 drain 后关闭。

计量固定为：ASR input_audio_ms 按验证后 accepted 的 canonical samples 计算；TTS input_text_codepoints 按首次冻结的文本区间计算，未冻结尾部撤销不计入；TTS output_audio_ms 按各 attempt 实际 generated 的 PCM samples 计算，包含取消前已生成但未交付的音频；delivered / played 只作独立诊断，不用于退还生成额度。重复 event、重复 commit 和重试回放不重复记同一输入区间，重试实际重新生成的输出仍是资源消耗。

冻结每个 TTS 单元后、进入引擎前预留其可强制约束的最大输出预算。这个上界来自已验证的 token / codec frame / 样本限制，不能只靠“生成太多后再断连接”。完成后按实际样本结算并释放余量；没有可强制输出上界的绑定声明 `hard_output_quota=false`，部署策略要求硬输出时长限制时不得使用它。累计额度按部署配置的日界线结算，跨日预留不能在两个日账本重复消费。

双工会话需要 ASR 和 TTS 能力时，一次原子预留所需的资源组合或会话配额；不得持有 ASR 等待 TTS，再让另一会话持有 TTS 等待 ASR。配额预留和实际 active compute slot 分离：活跃会话可保有已批准的实时容量承诺，计算片段按预算分派；承诺总量不超过已验证容量。

音色设计、克隆与校验复用相同准入流程，使用 voice-creation 或受控 batch 类别；创建声音不能免费绕过 ASR / TTS / 辅助阶段预算。其 manifest 申报完整阶段图，试听作为 TTS 消耗记账。首期不统计个人配额，不用浏览器标签分割预算；全局部署上限始终覆盖所有工作空间。

### 1.3 调度算法与公平性

1. 首先按入口、profile 与部署策略选择 `realtime`、`interactive` 或 `batch` 类别，再选兼容的模型绑定与资源池。
2. realtime 有明确保留容量，interactive 有有限等待预算，batch 使用其自有配额与剩余可用资源。保留比例由部署资格报告设置，不在业务预设中硬编码。
3. 同一类别内先按工作空间做加权 deficit round robin，再按任务意图和估算成本公平分派；realtime 在自己的保留范围内按最近 deadline 优先，batch 在成本公平下使用 FIFO + aging。
4. ASR 成本起点为音频时长，TTS 起点为文本长度 / 预估音频时长，辅助模型单独计价；执行反馈校准成本，不把 vLLM token 数作为唯一的跨 ASR/TTS公平单位。
5. 单任务不能把整本书一次性占满引擎。文件转写按有界音频单元、有声书按 render_unit 排队；单元结束是公平轮转与取消的检查点。
6. 默认不让 batch 借用 realtime 保留容量。以后开放借用时，必须有已测的最坏非抢占执行时间，且不超过实时剩余等待预算；没有证据就保持关闭。已经进入引擎的 batch 不能被假装瞬时抢占。
7. 各类别有自己的队列上限、最长等待和最小服务份额；不能用无限排队掩盖超载，也不能把不断 aging 的 batch 提升为突破 realtime 保留的最高优先级。

一次批任务重试仍参与原工作空间 / 执行类别的资源消耗与限速，不因生成新的 attempt 逃逸公平性。超限明确返回 quota / capacity / queue 错误；只有调用方与预设都允许时才使用显式的降级绑定，并将降级原因和实际 revision 写入 receipt。

### 1.4 多进程一致性与 fencing

每个 pool 的调度 owner 使用关系数据库持久租约与递增 fencing token，正式适配器为 PostgreSQL。容量预留在事务中原子完成，lease 绑定 operation、engine 与 token；所有回写都验证 attempt 与 fencing。API 副本不能使用各自内存中的 semaphore 充当全局准入。

管理面 `ResourcePool` 是跨节点聚合投影：`realtime_reserved_slots <= max_active_slots`，`active_slots + quarantine_slots <= max_active_slots`；任何计数都不能把隔离容量当成空闲容量。`status=ready` 必须至少有一个贡献节点和正容量，否则只能是 `degraded` / `unavailable`。聚合计数变化与节点租约账目保持同一事务口径，不能先展示可接单再补扣隔离槽位。受控应用事务必须使 host 与 instance 的 reserved / incremental / quarantine 投影等于实例基础预算与 admission lease 状态推导值；通过同一事务维护租约和预算、核对参与资源的当前状态/版本保证一致性，检查失败整体回滚。历史保护和最小写入权限继续保留，后台对账不能替代同步防超卖；不再强制由 PostgreSQL 延迟约束触发器完成跨表校验。

`ExecutionControlPort` 的权威实现是每个 engine / stage 的受控 execution gateway，它独占该引擎网络入口与凭据。gateway 将“核验当前 reservation、attempt、policy / 资源 revision 与 token → 写入唯一 submit intent → 放行本次提交”串行化；先将状态记为 submitting 并占住预算，再向引擎发送。每个 intent 有可查询的 engine request_id 或确定的引擎侧关联证据；submitting 与传输结果未知都仍占用资源，不能当作未开始而免费重试。

数据库 token 不能撤回已经发到不支持 fencing 的引擎请求，也不能单独阻止旧进程在检查后暂停、恢复后迟到发送。因此更换 execution gateway owner 前，必须先由旧 gateway 完成拒绝旧 token 的提交屏障，或确认旧入口进程已退出 / 被隔离；随后协调全部在途与 submitting 请求。若无法逐请求确认，则 drain / 重建受控引擎实例并使其旧连接失效，受影响请求显式失败。只有这个屏障与执行结束证据都成立，才能回收旧容量并启用新 owner。

网关或 worker 心跳失效后，调度器不能仅凭 TTL 到期回收算力：先由 execution gateway 通过 adapter 查询 / abort 对应执行，并拒绝过期 token 的新提交与结果发布。若无法证明旧提交入口已经封锁，或引擎不支持细粒度确认，隔离该 engine / stage 的容量，直到完成上述屏障与在途协调；该池暂不可用也不能重复分配仍被旧计算占用的资源。只在 worker 的库函数里提交前查询 PG 不满足此要求。

资源租约状态为 `reserved → executing → stopping → released`，不确定时进入 `quarantined`。Operation 结果状态与资源清理分开：cleanup_state 为 `not_required / pending / complete / quarantined`。只有确认停止才允许 cancelled；取消确认或 drain 期限到期但执行仍未知时，操作以 failed / cancellation_unconfirmed 终结，cleanup_state=quarantined，预算继续被占用且禁止自动重试。后台协调只更新清理事实，不反转唯一结果终态。`quarantined` 不是可重新调度的空闲状态。DB / 调度协调不可用时停止新准入，当前工作只在已有有限策略租期内运行，失效后停止外发并转入停止 / 协调；不能切到未协调的本地计数器继续接单。

隔离账目覆盖统一内存、设备内存、主机内存、CPU 解码、PCM 缓冲和 scratch 磁盘，并与 reserved 计数共同受节点 allocatable 上限约束。隔离量超过 `quarantine_threshold_ratio` 时，节点不得保持 `ready`，同一事务转入 `quarantined` 或 `draining`。退出隔离只能依据执行网关或节点控制器确认旧执行终止且资源释放的证据，或人工独立核验后的 `operator_verified_instance_terminated` 证据；释放事务必须同时扣减隔离账目、关闭 admission lease 并恢复对应槽位。人工释放还必须保存证据摘要、观察时间和原因，观察时间不得晚于实际预算释放时间；重复请求回显原事实，不重新解释证据。阈值以下不会自动恢复接单，仍须通过部署健康检查重新确认节点、入口和容量资格。

依照 ADR 0006，受控应用事务必须执行以下规则：`submit_intents` 只能在仍可执行的 admission lease 上插入；活动 `submit_intents` 不得附上停止证据；终态执行的状态、稳定错误码与停止证据不可改写；admission lease 只有在附属执行离开活动状态后才能进入 `released` / `quarantined`，`released` 不得遗留未关闭的 `unknown_quarantined`；隔离观察时间与释放证据不可改写；gateway fencing barrier 保持 sealed 且 token 不下降；engine generation 只有在所有 lease 已释放且没有非终态 intent 时才能转为 `stopped`。任一检查失败都必须回滚整个协调事务，不能先释放部分预算再等待后台补偿。数据库提供原子性、基础约束和最小权限，不再承诺独立拦截所有绕过受控入口的直接 SQL 跨表违例；所有正常运行写入必须经过明确事务操作，并接受同一套真实并发与故障测试。

### 1.5 准入与引擎内部 batching 的关系

企业队列只把已准入、受总量上限控制的请求送入引擎，引擎内部等候也计入 active / deadline 预算。不得同时在平台与 vLLM 各排无限长队列。模型跨副本路由在提交前完成，实时会话固定 owner；引擎故障后实时会话明确失败并新建 epoch，后台任务可以在取消旧 attempt 并完成协调后重试。

### 1.6 按需加载与实例回收

[ADR 0002](../decisions/0002-speech-models-engines-and-loading.md)规定：已启用实时 ASR、承担实时合成的 CustomVoice / Base 预加载并常驻，VoiceDesign 随音色创作任务按需加载、空闲回收，VAD 在 CPU 常驻且隔离会话状态。实时配方的复核及 required 辅助必须在准入前具备已验证的可服务条件；其余绑定的加载策略由部署资格决定，不能据此默认全部模型常驻或全部按需。

平台自动协调实例准备与回收，受限部署侧控制器通过 Compose / 运维基础设施执行批准实例的启停并报告事实。API 与 worker 不持有任意容器管理能力，不在业务进程加载大模型；模型推理入口、凭据、submit fencing、abort 和完成证据仍由 execution gateway 独占。

准备实例前，调度器在共享父物理预算中原子预留完整阶段的加载 / 热身峰值，包括设备与主机内存、临时缓冲和阶段间交接占用。共享物理内存不能被主机预算与设备预算重复当成独立余量。并发准备须共同受父预算约束；稳态预算只有在已取得对应证据后才可替换峰值预留。权重事先准备并锁定，执行请求不下载模型。

VoiceDesign 无预算时创建任务有界排队，不能挤出实时实例或借用实时保留；克隆和复用校验优先使用已就绪 Base，但仍申请音色创作的增量预算并参与公平调度。整个多阶段流程先确认资源方案，不允许先加载一部分模型，再无界持有资源等待剩余阶段。

回收先停止新分派并协调 gateway 的 submitting / 在途执行，满足 drain 与完成 / abort 条件后再停止实例。停止动作须与实例身份及当前调度 owner / fencing 对齐，防止迟到命令停止替代实例。空闲、心跳或租约超时都不能独立证明已停止；启动或停止结果未知时，相关预算保持隔离，旧执行未协调前不启动替代实例占用同一预算。

请求增量预算与实例常驻预算分别记账：请求完成仅释放已确认可释放的增量，实例预算在确认进程 / 容器停止且资源释放后才归还。加载失败也须完成清理证据；结果失败不等于资源已回收。实例卸载不删除模型绑定、音色候选、已发布 revision、来源或校验证据，再次启动重新核对 readiness 与资格匹配。

空闲回收期限、队列和准备时限由部署策略及后续机器契约确定，本节不新增数值默认或状态枚举。资格验证须覆盖 VoiceDesign 冷加载 / 回收与实时 1 ASR + 1 TTS 共存时的峰值占用、延迟、停止证据和预算不足行为。

## 2. 持久化、任务恢复与数据生命周期

### 2.1 逻辑数据模型

| 模块 / 数据集合 | 关键内容与约束 |
| --- | --- |
| 语音：deployments / workspaces / policies | 部署配置、默认分组、策略版本与生命周期；不含用户或组织目录 |
| 语音：profile_revisions / model_bindings / capacity_manifests | 不可变配置、验证记录与有效资格 |
| 语音：voice_drafts / voice_revisions / voice_validations / pronunciation_revisions | 音色候选、来源确认、兼容绑定、校验 / 发布状态、词典和后端映射 |
| 语音：assets / artifacts / artifact_dependencies | 所属工作空间、摘要、状态、存储引用、依赖与保留期限 |
| 语音：jobs / job_steps / attempts | ASR/TTS / 音色任务意图、检查点、执行 fencing、终态与错误 |
| 语音：operations / receipts | 执行身份、观察到的版本 / 参数与去重信息，不代表人的身份 |
| 语音：realtime_sessions / terminal_operations | 执行 owner、epoch、策略刷新租期与短期终态；不存 PCM |
| 语音：quota_reservations / usage_ledger / admission_leases | 全局与分组额度、实际用量、幂等结算、池容量 |
| 语音：idempotency_records / outbox / job_events / audit_events | 意图去重、可靠派发、任务事件与匿名 / 共享入口操作记录 |
| 工作台：projects / transcript_documents / transcript_revisions | 项目、声学正文引用、校对稿、辅助结果与来源版本 |
| 工作台：books / chapters / render_units / casting_revisions | 有声书项目、文本 / 角色依赖、局部失效及底座 Job / Artifact 来源 |
| 工作台：saved_assets / export_jobs / export_artifacts / export_events | 显式保存的项目副本、独立导出意图、工作台成品、幂等与恢复记录；不写底座 Artifact |

服务端 ID 不可预测，时间统一 UTC timestamptz，显示使用浏览器或部署时区。语音资源引用包含 workspace 以防误关联；工作台使用公开 Job / Artifact ID 和 client_reference 协调，不通过跨 schema 写表操纵底座状态。源转写制品属于语音底座，人工稿件和纪要属于工作台，可由明确保存操作复制快照，不能依赖同步请求的临时 receipt 保存全文。

按分页、外键、队列 claim 和唯一意图建立索引。首期 DDL 不建立用户、成员、授权或 RLS，不依赖未来身份迁移；身份体系是 [身份体系演进](../speechnexus-product-architecture.md#65-未来身份体系的独立演进) 的后续独立工程。具体 DDL、事务隔离和索引执行计划在 P0/P1 固化并验证。

### 2.2 作业与产物发布

Job 创建与 outbox 记录在同一数据库事务中提交；outbox 可能重复投递，worker 按 JobStep 的唯一执行意图和 attempt fencing 去重。正常状态迁移为：

```text
queued → running → succeeded
             ├→ retry_wait → queued
             ├→ cancelling → cancelled / failed
             └→ failed
queued / retry_wait → cancelled / expired
```

取消到达与成功提交竞争时，以数据库中先成功提交的合法状态转换为准：已经 succeeded 的任务不变为 cancelled；已经进入 cancelling 的任务不能发布新成品。取消请求响应回显实际终态，不能先显示“已取消”后又出现新完成结果。

停止确认在取消期限内成功，Job 才进入 cancelled。期限到期仍有未知执行时，Job 从 cancelling 转 failed，error.code=cancellation_unconfirmed；cleanup_state 汇总为 quarantined，并返回未清理 Operation 引用，相关资源继续隔离。后续协调只更新 cleanup_state / 资源清理事实，不将 failed 倒改为 cancelled；在旧执行清理完成前，自动或显式 retry 均拒绝为 `409 execution_cleanup_pending`。Job 查询与取消响应必须显示该差别。

每个步骤先写 intent，再执行；输出先写入带 attempt 的私有 staging 对象，验证长度、格式与摘要后，在单个事务中核对最新 policy / 资源状态、Job 取消状态和 attempt fencing，再插入 Artifact 引用、步骤成功和 outbox。与强制禁用 / 删除竞争按 [资源禁用与执行裁决](../speechnexus-product-architecture.md#64-资源禁用执行裁决与操作记录) 的同一数据库边界裁决；普通 retire 对已固定任务的行为也按该节执行。读者只能看到已提交 Artifact，不能看到 staging。失败的 staging 由有作用域的 GC 清理。断电发生在对象写入与数据库提交之间时，恢复只认已提交引用，孤儿对象不得被推断为任务成功。

系统提供 at-least-once 执行与幂等发布，不声称外部推理天然 exactly-once。仅在未向客户端交付实时输出且操作可安全重试、旧 attempt 已协调结束时自动重试；已产生部分实时音频的请求不后台自动重播。辅助步骤失败不覆盖成功正文，按 required / best_effort 策略决定整个 Job 是否成功。

失败分类包括输入 / 功能策略 / 参数永久错误、可重试后端错误、容量等待、执行超时、取消状态未知。自动重试默认最多 3 次、指数退避并加入 jitter，总自动重试窗口不超过 24 小时；永久错误不重试。quarantined engine 上的未知执行先协调，不直接换副本再跑。

用量账本区分 reservation、consumption 与未用预留释放。输入计量按稳定 operation 下的 canonical sample 区间 / 原始 Unicode codepoint 区间去重，键不包含 attempt；同一输入的 event 重发、commit 重发和作业内重试只计一次。TTS 字符为 JSON 解码后有效 Unicode scalar values，计用户正文的首次冻结区间，不将 instructions 或发音展开重复计入。输出以只追加的 `output_sample_intervals` 为权威账本，按 `(operation_id, attempt_id, output_stream_id, sample_interval, meter_kind)` 去重；数据库拒绝同一 meter 的重叠区间和既有区间改写 / 删除，因而失败 / 重试实际重新生成仍计消耗，标量水位不会覆盖先前证据。`samples + sample_rate` 是权威计量，音频毫秒只按 floor 派生用于展示。重试 API 创建的新 Job 是新意图，重新计输入。结果统计与基础设施消耗分别展示；无实际证据时保持待协调及预留，不伪造 0 成本或重复扣减。

### 2.3 保存与删除默认值

下面是初始保留策略，部署维护者可通过管理策略调整；不构成法规符合性声明。延长保存只影响尚未删除的内容，不能恢复从未保存的音频。

| 数据 | 默认行为 / 保留建议 |
| --- | --- |
| 实时 PCM / TTS 网络音频块 | 只在受限内存缓冲；不永久保存，不进入日志和调试导出 |
| 普通同步请求与结果 | 不保存原始音频或完整转写；幂等窗口仅保存意图与脱敏 receipt，不保证内容可重放 |
| 文件转写输入 | 用户上传即明确保存，默认 7 天；创建 Job 时检查与任务期限相容 |
| 普通生成音频 | 默认 30 天；显式保存到项目的成品按项目保留策略 |
| 会话转写 / 项目文本 | 默认仅经用户选择保存；选定工作空间自动保存策略时，开始前明确提示；建议 90 天 |
| 有声书原稿与成品 | 工作台随项目保存输入和已选择保留的结果副本，独立于底座临时 Artifact TTL；删除工作台内容按项目依赖执行 |
| 声音参考材料 / 特征 | 仅创建声音流程明确提交后保存，随 voice revision 生命周期与来源 / 使用确认记录管理 |
| 审计 | 建议 180 天，尽量只保存元数据；备份恢复后仍执行到期与删除规则 |
| Job 事件与终态元数据 | 建议 30 天；幂等记录至少符合 [错误与幂等](speech-api.md#16-错误分页与幂等)，活动意图不按短 TTL 删除 |
| 实时终态协调元数据 | 会话终结后 15 分钟；不构成历史音频恢复能力 |

执行中的输入与待发布成品使用有界引用租约，防止 TTL 在任务中途删除依赖；租约不能无限延长过期数据。创建时发现输入有效期不足，应拒绝或按部署策略显式延长引用保留。已发布声音所需的参考 / 特征材料形成随音色生命周期管理的已提交声音资产，不依赖原始上传输入的短期 TTL 或无限续租。

普通删除先列出有效的持久依赖与执行租约并拒绝破坏，包括当前无任务执行但仍被 VoiceRevision 引用的重建材料。明确级联 / 强制删除或强制禁用才优先于任务，按显式范围取消并协调清理，执行未确认停止前不释放预算或删除仍被使用的材料。

底座源 Artifact 删除或到期不自动追删已提交的工作台独立副本；项目删除按工作台自己的依赖和保留策略执行，需要跨模块级联时必须明确列出范围与确认点。

删除覆盖数据库、对象存储、缓存、搜索投影和旧 staging；备份按明确周期自然淘汰，并保存最小删除账本，恢复后在对外服务前重新应用删除与强制禁用。级联范围须显式列明，不能悄悄破坏其他项目或独立保存副本。内容摘要只在部署 / 工作空间内去重；共享分组不构成保密空间，不输出可用于任意探测内部对象的存储查询接口。

### 2.4 上传与网络边界

文件以有界流上传至私有 staging，检查 magic bytes、MIME、大小、解码后时长和采样参数后转为 ready；解码器运行在无网络、有限 CPU / 内存 / 时间的受限进程。只接受 allowlist 格式，拒绝归档炸弹、未知序列化权重与用户提供的本地路径。首版不提供任意远程 URL 导入、动态下载模型或外部回调 URL。

模型与 tokenizer / codec 文件在部署准备阶段离线准备，核对来源、revision、文件集与摘要后发布绑定。voice 特征采用经过校验的安全格式与已批准生成流程，不允许上传任意 pickle / 代码。声音素材的来源与使用确认作为资源元数据记录；访问与删除遵守当前部署功能策略和依赖裁决，不宣称已有按人授权。

工作台 LLM 仅接部署批准的本机 / 内网服务，内容不得出网；普通请求不能指定端点，内网代理也不能将业务内容转发到公有云。该约束同时适用于观测导出和浏览器资源。模型与镜像的制品准备独立于业务运行，运行时不依赖外部下载。若内网 LLM 与语音服务同机，其占用计入部署物理预算，不能成为实时保留之外的隐形竞争者；见 [ADR 0003](../decisions/0003-workbench-llm-network-boundary.md)。


主动删除的机器入口、依赖版本 pin、确认范围、异步清理状态、独立副本和恢复屏障
见[数据删除与依赖裁决](data-deletion.md)。容量每槽六维下限、运行角色权限与同步
事务投影见[应用事务方案](coordination-transactions.md)和[精简协调草案](distributed-coordination-schema.md)，
删除不得清除协调与执行证据。
