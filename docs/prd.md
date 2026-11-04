# SpeechNexus 产品需求

## 产品目标

在自有部署网络内提供独立语音底座、管理端和工作台，支持文件与实时语音、声音资源、可靠执行及内容生命周期，并在基础能力之上交付复合业务场景。系统支持从单节点到物理集群的一致协议、持久状态和恢复路径。

## 非目标

首期不实现登录、用户目录、RBAC、ACL、多租户或个人私有空间；管理端与工作台不构成身份或数据隔离。语音底座不依赖工作台业务或 LLM；工作台通过公开语音协议消费底座，不共写底座表。

## 全局约束

- PostgreSQL 是正式产品数据与协调权威，对象存储保存明确提交的输入与制品；Redis 仅可作可丢失缓存或唤醒，恢复依赖持久记录和轮询。
- 跨进程持久输入和制品经对象存储传递，实时内容使用约定的网络流协议；本地临时缓冲有界且私有，不通过跨容器共享目录传递内容。业务 API、worker 和工作台不直接持有任意容器管理权限或引擎凭据。
- 完整配方的准入、预算、租约和 Job/outbox 保持原子边界；提交固定命令与资源版本，结果未知按原身份恢复。取消或终态不等于停止；有外发或外发未知时按契约取得可信停止与释放证据后结算，确认未外发的分支另按原事务核验。
- 内容删除须固定版本和摘要预检、明确确认并持久记账；恢复先重放删除账本，再开放内容，独立副本按所属边界处理。
- 内容不得离开部署网络；内网 LLM 默认关闭，仅使用服务端批准的本机或内网服务，失败不回退公有云。公开契约统一生成或校验消费者类型，避免独立语义副本。

## 需求归属与协作

以下十个 Epic 以产品能力划分唯一主责。跨领域协作以固定版本、公开契约、提供者输出和消费者接收条件衔接；共享前置不改变需求归属。具体实施任务按需创建，每项直属一个主 Epic。

## 产品接收与放行

各领域应完成适用范围内的生产接线、真实消费者、故障恢复、质量与容量及证据；联合接收还须满足目标物理拓扑、许可和运维交接。组件、fake、CI、单机逻辑集群或隔离探针不能替代真实业务和物理多机资格。最终产品由用户明确放行；文件能力在联合证据与提供者、消费者确认齐备后，仍有其独立的用户放行决定。

会议与纪要、语音助手、有声书、实时字幕和提词在其他功能完成后推进；共享语音、媒体、删除恢复与发布准备按各自主责继续。排期不改变能力归属。

<a id="epic-337"></a>
## Epic 337 · 文件语音与底座内容任务

让调用方可靠提交音频或文本，取得可恢复、可下载的识别和合成结果，并管理内容生命周期。

**范围：** 底座 Asset、Job、Operation、Artifact 生命周期，文件 ASR/TTS、静音、通用 VAD、对齐与匿名分人。

**边界：** 执行和资源结算、实时会话及工作台独立副本由相应领域负责。

**领域接收条件：** 公开文件识别和合成从受控输入、持久任务、执行到制品发布与下载形成可恢复闭环；参数、格式、文本/音频上限、源时钟、质量和辅助结果按声明范围验证。空语音有契约规定的结果和可信零外发证据；删除经版本固定预检、确认及账本执行，独立副本按其所有权处理。正式 SDK 与页面消费同版结果，失败、重放、取消和重启均有相应证据。

通用 VAD、对齐和匿名分人供文件与实时语音共同使用。文件能力联合证据及提供者、消费者确认齐备后，其产品放行仍由用户明确决定；常规实现与必要验证按已有授权推进。

<a id="f-asset"></a>
### F-ASSET · 受控输入资产与媒体检查

**需求：** 内容上传、音频有效检查与固定对象版本。

**验收：**

- 音频 ready 有真实检查事实，不按 MIME/扩展名或文本检查推断；同键恢复并固定对象版本和实际生效期限。资产默认 TTL 为 7 天，可由部署配置或请求调整；非必需临时材料和取消清理按策略处理。 当前默认资产工厂使用文本检查器，音频 ready 尚须接入真实音频检查并联验。
- 对象长度/摘要核验后原子提交数据库引用；清理须固定对象版本并确认安全条件，写入结果未知或写者停止未确认时保留原身份和账本协调，不能因失败直接删除或重写。不将对象存储与数据库视为同一事务，不接受客户端指定路径、对象 key/URL，已提交内容经受控入口读取。以真实 HTTP 与隔离 PG/版本化 MinIO 测试重启及过期。

设计依据：[speech-api.md](architecture/speech-api.md)、[runtime-governance.md](architecture/runtime-governance.md)。

<a id="f-asr"></a>
### F-ASR · 文件识别与源时钟

**需求：** 有界解码、源样本映射、转写与辅助结果关联。

**验收：**

- 长/短音频、截断、边界超限、重采样和分段结果保留可核对源时钟；无界输入被拒绝，真实引擎与公开调用同版验证。
- 语言、prompt、温度、切段与期限由同源固定命令和 binding/策略身份解析；未实现参数在对象读取或外发前拒绝，取消、过期及改写防护有效。
- 长文件、尾部和无语音输入的解码、重采样和切块有界；源/模型/wire 时钟及真实时间戳一致，交叠不重复正文，辅助结果只挂匹配正文 revision。
- 真实取消和停止有资源释放证据及稳定错误；request cleanup、ID 消失或 unfinished=0 不能代替 GPU 停止与释放。

设计依据：[speech-api.md](architecture/speech-api.md)、[runtime-governance.md](architecture/runtime-governance.md)。

<a id="f-tts"></a>
### F-TTS · 标准文件语音合成与参数硬上界

**需求：** 真实合成、受控输出及 binding 指令与输入长度。

**验收：**

- CustomVoice 的一次 intent 对应一次合成，绑定的输入文本最多 400 Unicode codepoints；该限制不是 token 数，也不推广为其他模型或公共任务的通用文本上限。
- `nexus.overrides.tts.instructions` 入口上限为 2000 codepoints；标准 `SpeechRequest.instructions` 和内部执行命令 `instructions` 上限均为 4096 codepoints；固定 pipeline 的 `instructions` 默认上限为 500 codepoints。分别验证实际入口校验、配置传递与 2001–4096 准入结果，不静默截断。
- 冻结文本、参数、声音和版本；超限明确拒绝，WAV/MP3/PCM 输出格式、长度、样本数与回执一致，失败不泄露未提交制品。
- 目标是在请求提供 `max_output_audio_ms` 时收紧执行 policy 和 PCM 上界；保留 100ms 请求不得接受 200ms 结果的反例，覆盖恰好上限、超一帧、低于 binding、重放及编码制品。当前同步 `SpeechRequest` 尚无该字段，现行上界来自部署策略；须先补齐契约再验证请求级限制，已有收紧修复不代表完整矩阵已通过。

设计依据：[speech-api.md](architecture/speech-api.md)、[runtime-governance.md](architecture/runtime-governance.md)。

<a id="f-client"></a>
### F-CLIENT · 文件语音公开调用及端到端结果

**需求：** 标准 SDK 同步和持久任务的公开转写与合成可独立调用。

**验收：**

- 标准 SDK 无 nexus 包装支持声明的 ASR/TTS；合成默认 MP3，显式 WAV/PCM 及错误符合契约，同步与 Job 使用同一规范命令，不以协调快照冒充公开 Job。
- 同版真实上传音频获得转写、固定文本/声音获得合成；同步入口按契约直接返回结果，持久 Job 的结果经受控 staging 和 Artifact 发布后供 SDK 下载。工作台支持导入、校对、导出及试听下载；未发布的私有 staging 不可公开读取。
- 持久意图及身份在外发前保存，HTTP 在事务外；远端已受理、本地记录前崩溃及回执丢失均只按原键恢复，错 ID/版本/类型不成功结算。当前同步 ASR/TTS 入口拒绝客户端 `Idempotency-Key`，内部恢复能力不能视为客户端可重放；须补齐契约要求并验证。
- 覆盖正常、无语音、取消、迟到、重放、真实引擎/控制器重启、两个逻辑节点竞争及基础删除；停止和释放双证据齐全才退预算。
- 文件 ASR 可省略 profile，但必须固定权威 binding 与显式默认；公开 SDK、持久 Job、受控网关、结果发布和工作台按同版证据贯通。
- 文件能力联合证据及提供者/消费者确认齐备后，其产品放行仍由用户明确决定；常规实现和必要验证按已有授权推进。

设计依据：[speech-api.md](architecture/speech-api.md)、[runtime-governance.md](architecture/runtime-governance.md)。

<a id="f-job"></a>
### F-JOB · 任务与 Operation 的公开生命周期

**需求：** 任务查询、事件、取消、重试及失联恢复。

**验收：**

- 查询、取消、重试、Operation/receipt 与 SSE 的权威状态一致；断线时以持久事件序号和 `Last-Event-ID` 从该序号之后续读，事件历史过期返回 `410 event_history_expired` 后读取当前 Job 状态，不伪造历史；幂等与取消语义一致。
- 显式重试创建新的 Job 及独立 attempt 历史，不复活旧 Job；旧执行尚未确认停止时返回 `409 execution_cleanup_pending`。当前公开 Job 路由仅装配创建与读取，取消、重试和 SSE 仍须接线验收。

设计依据：[speech-api.md](architecture/speech-api.md)、[runtime-governance.md](architecture/runtime-governance.md)。

<a id="f-artifact"></a>
### F-ARTIFACT · 底座制品发布与下载

**需求：** 固定资源、私有 staging、可控下载、保留和过期。

**验收：**

- 受控 Asset/Artifact 经对象长度/摘要与固定版本检查；staging 不可公开，最终发布与 Job/attempt/fencing、取消和策略裁决原子一致。
- 固定 revision 的下载及 Range、保留和过期可复核；同步/实时默认不保存内容，Job 结果按固定策略保存；普通生成音频目标默认保留 30 天，其他内容按各自部署及保留策略处理。

设计依据：[speech-api.md](architecture/speech-api.md)、[runtime-governance.md](architecture/runtime-governance.md)。

<a id="f-delete"></a>
### F-DELETE · 底座内容预检、确认及删除账本

**需求：** 底座内容按所有权删除，独立副本有独立生命周期。

**验收：**

- 预检固定版本/摘要和确认范围，独立的 `Deletion` 资源、ledger、tombstone 与 outbox 幂等恢复；音频沿用 TTL、原键、对象版本，只清理由本模块持有的对象和命令正文，不追删独立副本。 当前资产到期清理账本不等于用户请求删除；Speech 删除路由和跨模块停止协调尚未实现。
- 相同删除意图/摘要重放保留原时间版本；冲突明确返回，提交未知仅查原身份并以原许可确认，不重复外发；协调、计量和停止事实保留。执行网关内部的 `deletion_job_id` 是清理许可关联字段，不把公开删除资源变成推理 Job。
- 经预检并明确确认的级联删除或适用的资源状态变更、revision 推进与通知 outbox 在同一权威事务提交；普通删除先检查有效持久依赖和执行租约，禁止未经确认地破坏，不提供绕过停止证据的强制标志。

设计依据：[speech-api.md](architecture/speech-api.md)、[runtime-governance.md](architecture/runtime-governance.md)。

<a id="f-aux"></a>
### F-AUX · VAD、对齐与匿名分人

**需求：** 通用媒体辅助事实固定正文和样本版本，供文件及实时路径消费。

**验收：**

- 并发会话相互隔离、缓冲有界，文件/实时消费者正确处理切段版本及源样本映射；无语音终态依可信执行事实裁决。
- CPU VAD 以 16 kHz 单声道 PCM16 和有序样本输入，前缀缓冲与策略 revision 固定；其模型时钟到 24 kHz 公共坐标及源文件的映射记录真实重采样影响，不能把简单比例投影当完整源时钟。该输入规格不替代其他辅助模型各自的格式要求。
- 词级时间戳映射源样本，交叠不重复，错版本或旧代结果拒绝。
- required 辅助纳入完整配方和终态，冻结语料、阈值及逐 binding 支持矩阵和质量证据；证据缺失时明确拒绝，不隐式降级为 best_effort，并按真实 ASR 场景联验。
- 支持至多八人；不能恢复时显式标明身份连续性边界，旧结果不得覆盖新正文。

设计依据：[speech-api.md](architecture/speech-api.md)、[runtime-governance.md](architecture/runtime-governance.md)。

<a id="f-async"></a>
### F-ASYNC · 文件任务固定配置与异步执行

**需求：** 公开 Job 固定解析结果、执行身份和制品版本。

**验收：**

- 真实工作进程启动并消费持久 Job；解析、准入、执行、发布和终态均固定原 revision、identity 与 fencing，禁止重复外发或重放后重发布。
- 分别验证提交、成功终态、失败及提交结果未知；配置缺失继续 fail-closed。

设计依据：[speech-api.md](architecture/speech-api.md)、[runtime-governance.md](architecture/runtime-governance.md)。

<a id="f-silence"></a>
### F-SILENCE · 文件空转写公开结果

**需求：** 合成静音从公开转写 API 到 SDK 按空结果契约交付。

**验收：**

- 公开结果符合空结果契约；原请求、有效命令、可信 gate、固定对象及终态完整关联，实际引擎零外发。
- 原执行及计量按契约闭环；超时、取消和提交未知准确呈现，不以单测或 VAD 空输出代替。

设计依据：[speech-api.md](architecture/speech-api.md)、[runtime-governance.md](architecture/runtime-governance.md)。

<a id="f-shared"></a>
### F-SHARED · 标准接口与共享数据分组

**需求：** 公开文件资源以 workspace 作为内容和预算分组，不作为用户身份或安全边界。

**验收：**

- 标准 REST 不要求 nexus/workspace/工作台字段；显式 workspace 的引用和版本必须匹配。
- 同一共享入口不宣称个人私有内容、ACL 或租户隔离。

设计依据：[speech-api.md](architecture/speech-api.md)、[runtime-governance.md](architecture/runtime-governance.md)。

<a id="epic-338"></a>
## Epic 338 · 执行、算力与停止结算

使执行准入、网关和节点生命周期可恢复，未知状态下不重复执行、不提前归还资源。

**范围：** 完整配方原子准入、调度、网关、节点实例、租约预算、真实停止、释放与结算。

**边界：** 文件或实时业务语义、模型资格及绑定生命周期由相应领域负责。

**领域接收条件：** 完整命令和固定身份持久化后才外发；准入覆盖完整配方、预算、租约和 Job/outbox 原子边界。提交未知按原身份恢复并保留占用；取消、超时或断线不构成停止证明，匹配的停止及资源释放证据经原事务仅结算一次。受限节点完成预热、持续就绪、峰值/稳态、排空和崩溃恢复；零外发门控、封闭及结算可信可核验。

<a id="x-admit"></a>
### X-ADMIT · 配方原子准入与公平调度

**需求：** 按权威策略和完整配方预留 quota、预算、lease 和持久任务。

**验收：**

- 完整 required 配方的 attempt quota、父 host/instance 预算、全部阶段 lease、Job 与 outbox 在同一数据库事务全部提交或回滚。
- 跨池争夺最后预算、多 Worker/两 Scheduler 与实时保留共享父预算且有界公平；覆盖全部业务 quota/结算。
- 重复 claim/outbox 不重复执行；写入和发布事务重核 worker、claim_version、owner、期限；旧 owner 在入口屏障及在途协调完成前不能激活。
- 18 个协调端口及完整事实快照由生产消费者联合核对；PostgreSQL 是正式产品权威，InnoDB 仅作关键协调路径对照；双工、跨池、业务 quota 与结算的相关共享行为无跳过。

设计依据：[runtime-governance.md](architecture/runtime-governance.md)、[technical-blueprint.md](architecture/technical-blueprint.md)。

<a id="x-recover"></a>
### X-RECOVER · 投递、租约与计量恢复

**需求：** 原身份投递、持续 reconciliation、输出区间及一次结算。

**验收：**

- Redis 停用或通知丢失仍可依持久记录与轮询推进；多 Scheduler/Worker 覆盖正常、取消、接管、崩溃和未知。
- 提交结果未知仅查原身份，不新建身份、重复外发或提前退预算；原结果查询失败保持 unknown，真实停止与释放证据齐全后只释放一次。
- 结算消费生成区间而不复制 PCM 账本；观察、投递与恢复均能区分真实证据和合成 fixture。
- 18 端口及事实查询的完整 value、所有权和实际消费者有证据；内部 JobFact 不伪装公开业务 Job。
- 锁序、锁后数据库时间与版本前提有效，运行角色非 owner 且不能越权；确认回滚与提交未知分开，读取失败保持 unknown/query_original。
- PG/InnoDB 相关共享协调行为相同且无跳过，真实业务消费者与 fake/原型分列；音频资产与完整业务任务保持同库事务边界。
- 有外发或外发未知时，按原事务核验匹配实例 generation 的可信停止及资源释放证据；确认未外发的分支按独立条件裁决。不能直接归还预算或凭对象存在写成功。
- 失败保留隔离；审计不含私有正文。
- 提供受支持的产品内 unknown / reconciliation_required 定位与恢复入口，核对运行权限、固定版本和代次并保留审计；只读 runbook 或人工 SQL 不代替产品入口。

设计依据：[runtime-governance.md](architecture/runtime-governance.md)、[technical-blueprint.md](architecture/technical-blueprint.md)。

<a id="x-gateway"></a>
### X-GATEWAY · 受控网关提交、取消和停止证据

**需求：** 完整命令先持久化、最终 fencing，真实请求级停止与释放证明。

**验收：**

- 持久化完整固定命令及规范摘要后才外发；同意图、提交未知和取消重放查询原身份，不重复外发或伪造 dispatch 时间。
- 取消受理、断线、TTL、请求消失及观察指标均非停止/释放证据；unknown 持续占用，按原事务核验可信停止与释放双证据后结算。确认未外发的失败须满足 `not_required` 的证据条件，不能用该分支解除 unknown 或伪造停止。
- 公共 cancelled 与内部 unknown 分开；仅 cleanup complete/not_required 可发布公共 cancelled，确认丢失及重启按原执行和固定对象恢复。

设计依据：[runtime-governance.md](architecture/runtime-governance.md)、[technical-blueprint.md](architecture/technical-blueprint.md)。

<a id="x-node"></a>
### X-NODE · 批准实例与预算生命周期

**需求：** 受限模板、generation、热身、drain 与可信资源释放。

**验收：**

- 只允许批准模板和固定 node/instance/generation/配置摘要，拒绝任意镜像、挂载、shell；迟到 stop 不误停替代代次。
- 固定容器 IP/路径真实预热通过前保留 loading/峰值预算；503、重定向、超时非 ready，不经代理；要求整个 wall-clock deadline 有界且多 IP 不重复预热。当前实现分别约束地址选择、单次预热和 daemon 调用，尚不能由这些期限推定整个动作存在硬截止，须另行验证该验收条件。
- Memory/MemorySwap 上限由 host/统一内存 peak 保障，资格通过才转 steady；drain 拒绝新提交，退出、释放、终结及所有 lease 清理后才退 base，unknown 隔离。
- 恢复依据持久意图、检查点及可信节点证据，诊断快照无事实权威；key 不替代 TLS、fencing、网络隔离。
- 升级批准模板后，旧代实例仍可按原执行身份、原预算与原 runtime 接收 STOP；旧代请求不误停新代。

设计依据：[runtime-governance.md](architecture/runtime-governance.md)、[technical-blueprint.md](architecture/technical-blueprint.md)。

<a id="x-binding-stop"></a>
### X-BINDING-STOP · 策略与资源撤销的运行态传播和停止

**需求：** 消费 policy/binding/voice/asset 的固定 revision 撤销事件，阻断新增执行并按原请求协调停止与释放。

**验收：**

- 权威 policy、binding、voice 或 asset 的强制禁用或经确认删除先提交时，按固定 revision 事件和确认范围主动通知网关与 worker；最长 30 秒策略刷新租期仅作传播兜底。
- 收到适用撤销后停止新 credit 与外发字节，并按原请求身份取消已确认范围的在途和排队意图；事件派发后、实际调用前崩溃时可按原身份恢复。当前网关已提交取消 dispatch 标记后、实际外发前崩溃时，只能保留 unknown 并查询原身份，不能保证自动补发取消。
- 新准入、最终 submit intent 和 Artifact 发布的裁决事务始终读取权威状态或等价可串行化版本；不得把 30 秒刷新期限当作准入或发布宽限。
- unknown 持续隔离且占用预算；仅匹配可信停止与资源释放双证据后由原事务结算一次。无停止事实或无请求记录均不得伪造停止、成功或释放。

设计依据：[runtime-governance.md](architecture/runtime-governance.md)、[technical-blueprint.md](architecture/technical-blueprint.md)。

<a id="x-zero"></a>
### X-ZERO · 可信零外发、封闭和原事务结算

**需求：** 可信持久门控、受管外发与空结果关闭的原身份恢复。

**验收：**

- 调用方声称或缺少请求记录不能证明零外发；外发与取消竞争只能产生契约允许的唯一结果。
- 未知保留占用，原身份与锁序贯穿恢复；门控未完成不得启用正向静音结算。 当前零外发契约仅提供候选类型和校验，可信 gate、受管外发及正向 seal/成功结算尚未接入；已有取消接收和防伪释放边界不能代替该链。
- 封闭证据关联原执行、generation 和原命令摘要及固定对象；不得凭无请求记录宣称成功。
- 许可、封闭、终态与原租约结算不能跨库双写；unknown 或冲突保持隔离。
- 重复恢复不重复发布或释放；停止和资源释放要求不因空结果降低。

设计依据：[runtime-governance.md](architecture/runtime-governance.md)、[technical-blueprint.md](architecture/technical-blueprint.md)。

<a id="x-redact"></a>
### X-REDACT · 执行命令删除许可与不可变 tombstone

**需求：** 可信删除协调授权和清理执行私有正文，不删除执行/用量证据。

**验收：**

- 服务端验证权威删除意图及依赖，不把内部 `deletion_job_id` / digest 当授权；pool→lease→submit 锁下重查身份、清理终态和 lease release，正文与不可变 tombstone 原子提交。
- 真实消费者验证 redact 许可、版本和 tombstone，保留最小协调、计量和停止事实；单请求停止与整代停止分别取证。
- 当前网关已有受信清理事务边界，但默认没有许可签发器或 HTTP 删除入口；完整删除协调器与真实消费者仍待接入验收。

设计依据：[runtime-governance.md](architecture/runtime-governance.md)、[technical-blueprint.md](architecture/technical-blueprint.md)。

<a id="x-req"></a>
### X-REQ · 请求控制与外发前参数拒绝

**需求：** 网关识别冻结的提交命令、身份、策略及不支持参数。

**验收：**

- 语言、prompt、温度、切段与期限从固定命令和 binding/策略身份解析；不支持的参数在对象读取或外发前拒绝，取消、过期和改写不得发布无效结果。
- 固定输入、持久准入、唯一外发、发布与 SDK/页面消费联验同一场景的重放、取消及重启。

设计依据：[runtime-governance.md](architecture/runtime-governance.md)、[technical-blueprint.md](architecture/technical-blueprint.md)。

<a id="x-realtime-reserve"></a>
### X-REALTIME-RESERVE · 多阶段峰值与实时预留

**需求：** 完整配方跨 host/instance 与实时预留统一准入；不得后台挤占实时容量。

**验收：**

- 配方启用 1.7B 全段复核时，该阶段与 required 辅助在准入前可服务并计入全部阶段预算；未启用全段复核的配方仍须计入其 required 辅助。双工必须整体准入或整体回滚。 当前准入事务具备多阶段预算与实时保留规则；公开实时双工链和公平调度尚须接通并联验，事务组件不能单独证明实时资格。
- VoiceDesign 加载/热身/执行峰值事先预留；不足排队，不借用实时保留；只在停止与释放事实成立后归还。

设计依据：[runtime-governance.md](architecture/runtime-governance.md)、[technical-blueprint.md](architecture/technical-blueprint.md)。

<a id="epic-339"></a>
## Epic 339 · 实时语音会话与双工流

让应用以有界资源使用实时识别、实时合成及双工会话，并在中断后清楚知道恢复边界。

**范围：** 实时 ASR/TTS、双工会话、时钟、事件、流控、背压、恢复与终态。

**边界：** 通用媒体辅助、执行资源及具体复合业务由相应领域负责。

**领域接收条件：** 真实流式 ASR/TTS 与正式消费者同版联验；预览与最终结果、唯一逻辑终态、冻结文本顺序及样本/文本时钟符合契约。旧 owner/epoch、重复或越界帧不推进新流，缓冲和输出上限可核验；断线、停止及恢复明确暴露 gap。双工和 required 辅助按完整资源配方准入，区间精确重放不重复计量并与真实生产者一致。

<a id="r-asr"></a>
### R-ASR · 实时识别与显式 gap 恢复

**需求：** 会话协商、预览/终稿、owner/epoch、水位、终态及连接恢复。

**验收：**

- preview 与 final 分离，空成功、唯一逻辑终态和正文 revision 符合契约。
- segment 的正文 `completed/failed` 与整体 `nexus.operation.done` 分开验收；required 辅助失败时整体失败，不改写已经发布的正文。best_effort 辅助使用独立子执行，迟到结果只更新对应正文 revision 的辅助状态，不反转父终态。
- 旧 owner/epoch 和重复输入不能推进新流；样本/文本水位、credit 与缓冲有硬上界。
- 断线后按目标协议建立新 session / epoch，保留已确认文字并显示输入 gap；不进行未保存 PCM 的透明重放，也不自动重播旧 TTS。旧执行的终态与资源释放仍按原身份协调。
- 真实 ASR/TTS 流、VAD、网关和真实设备消费者按可对应集成版本验证 owner/epoch、样本/文本水位、credit、背压、恢复与可信停止，所需参与方确认后提交用户放行。
- ASR 正文 final 从 segment_closed 到 completed/failed 的 P95≤2000ms 为 PRD 候选目标，包含最终复核等待、required 辅助延迟另报；须按 binding、语料与负载冻结后测量，不宣称已达标。

设计依据：[speech-api.md](architecture/speech-api.md)、[cpu-vad.md](components/cpu-vad.md)。

<a id="r-tts"></a>
### R-TTS · 实时语音合成与双工会话

**需求：** 冻结文本驱动实时 TTS，协调完整双工。

**验收：**

- 严格遵守 append/commit/finish 及冻结文本；全部双工阶段配额、预算、租约、Job/outbox 同事务准入。
- owner/epoch、输出水位、credit 和缓冲有界；真实停止、断线、gap 与恢复由正式消费者联验。
- 按身份、seq、epoch、descriptor 关联控制与 PCM，拒绝旧帧、迟到和越界；credit、背压、缓冲和硬输出上限在真实流生效，不能以文件切片代替。
- 权威流锁拒绝部分重叠，精确重放不重复计量；真实生产者区间一致，分别记录首生成 PCM、网络接收和播放结束，不直接作为业务计费。
- 同一会话最多一个活跃 TTS utterance；取消后可立即停止本地播放，但新计算须等待旧租约确认释放。成功终态不证明浏览器已收到全部音频或完成播放；公开 `text_credit` schema 尚未规定单位，正式联验前须明确生产者与消费者的计量单位。
- 启用的 1.7B full_segment 和 required 辅助纳入完整配方；双工、停止与恢复联验。

设计依据：[speech-api.md](architecture/speech-api.md)、[cpu-vad.md](components/cpu-vad.md)。

<a id="r-qual"></a>
### R-QUAL · 实时双路与声音加载混合资格

**需求：** 面向指定语料、负载、计时的混合性能与容量。

**验收：**

- 同时运行 1 路实时 ASR 与 1 路实时 TTS，并独立测 VoiceDesign 冷加载、热身、执行、回收的影响。
- ASR 从服务端接受构成最低识别窗的最后一个 PCM 样本，到实时网关写出首个有效预览，P95≤1000ms；TTS 从首个可合成文本单元被 commit/finish 接受并冻结，到交付 transport 写出首个完整非空 PCM 帧，P95≤800ms。网络接收及播放耗时另报。

设计依据：[speech-api.md](architecture/speech-api.md)、[cpu-vad.md](components/cpu-vad.md)、[模型与加载策略](decisions/0002-speech-models-engines-and-loading.md)。

<a id="r-pcm"></a>
### R-PCM · 实时 PCM 时钟与终态

**需求：** 协商输入输出 descriptor、样本水位和 credit，显式会话终态。

**验收：**

- 按原样本时钟去重、拒绝旧 epoch；有界队列和输出硬上限不允许静默丢帧。
- 预览可修订且不冒充终稿；关闭、失败或取消产生唯一逻辑终态，不把客户端播放事实冒充引擎停止。

设计依据：[speech-api.md](architecture/speech-api.md)、[cpu-vad.md](components/cpu-vad.md)。

<a id="epic-340"></a>
## Epic 340 · 声音与发音资源

让用户创建、确认并安全复用声音，同时使发音资源可固定版本、受控管理。

**范围：** 声音设计、克隆、试听、资格校验、发布、退役及发音词典版本。

**边界：** 模型绑定和执行、基础界面由相应领域负责。

**领域接收条件：** 设计与克隆分别使用固定材料完成候选、试听、新文本复用、明确确认与发布；已发布声音供真实后续 TTS 固定引用，重启按材料重建，缺失或不兼容明确不可用。退役与新引用竞争保持锁序、合法在途和原身份重放语义；发音资源支持版本化管理、发布和受控引用，并与相关消费者联验。

<a id="v-design"></a>
### V-DESIGN · 描述设计与声音候选复用

**需求：** VoiceDesign 候选、试听、换文复用资格与恢复。

**验收：**

- preview_only 与可发布资格分离；同键同意图重放固定原意图。
- 缺失或不兼容材料时明确不可用，不以重新设计替代恢复；加载、热身和回收遵守 peak 预算及可信停止。

设计依据：[speech-api.md](architecture/speech-api.md)、[data-deletion.md](architecture/data-deletion.md)。

<a id="v-clone"></a>
### V-CLONE · 参考材料克隆和恢复

**需求：** 克隆材料、文本确认、重建及同键原意图。

**验收：**

- 参考样本须明确确认使用；公开契约的 `reference_text` 可省略或为 null，具体绑定可要求提供。提供文本时须固定并确认其与样本的对应关系；材料变更重新确认，试听不冒充可复用资格。
- 材料缺失或不兼容明确不可用，不通过重新克隆替代恢复；加载、热身和回收遵守峰值预算及可信停止。
- 同键同意图固定原参考样本/文本、材料摘要及资源/binding版本，返回原意图且不重新克隆；同键异体返回明确冲突。

设计依据：[speech-api.md](architecture/speech-api.md)、[data-deletion.md](architecture/data-deletion.md)。

<a id="v-publish"></a>
### V-PUBLISH · 声音版本发布、退役与引用竞争

**需求：** 不可变 VoiceRevision 生命周期及 Job 引用的资格屏障。

**验收：**

- preview_only 不能发布；真实后续 TTS 固定已发布引用，不能使用候选或可变默认。
- 发布、退役、禁用、删除及在途使用发生版本竞争时按契约裁决，完整预算、Job/outbox 保持原子。
- 声音作为不可变版本受控引用，删除与已有独立副本按各自所有权处理。
- 退役先提交拒绝新引用，Job、operation、outbox 无部分写入、无多余预算或租约；新引用先提交仅持有效依赖凭据的合法在途执行继续。
- 同键同意图返回原结果、不新建或重复执行；不同意图冲突，历史精确读取保留。
- 覆盖不同 binding 共用 voice、两种提交顺序、回滚与恢复；未装配入口或未测拓扑不宣称通过。
- 设计与克隆两种模式分别获得试听、新文本复用、明确确认和不可变发布；真实 Base TTS 固定版本，材料与 binding 支持重启重建，不兼容时明确不可用。
- 声音退役、禁用或经明确确认的级联删除的权威状态及 revision 和通知 outbox 在同一事务提交；默认退役禁止新引用，持有效依赖凭据的在途任务按契约完成，删除不得绕过停止证据。

设计依据：[speech-api.md](architecture/speech-api.md)、[data-deletion.md](architecture/data-deletion.md)。

<a id="v-lexicon"></a>
### V-LEXICON · 发音资源生命周期

**需求：** 发音词典草稿、不可变 revision、发布及引用。

**验收：**

- 词典草稿、已发布 revision 与默认指针分离；创建、列表及固定 revision 读取按公开契约可用，历史版本不可被覆盖。
- 消费任务固定发音 revision；发布/退役和默认切换遵守版本前提，不改变已固定任务。
- 发音资源发布先校验资格；expected_current_revision 在发音资源当前 head 锁及同一事务内比较，不用 MAX 聚合代替 CAS；并发激活仅一个成功，退役发音资源不进入默认解析。

设计依据：[speech-api.md](architecture/speech-api.md)、[data-deletion.md](architecture/data-deletion.md)。

<a id="v-reference"></a>
### V-REFERENCE · 声音材料与发布确认

**需求：** 区分样本材料、候选试听、验证和不可变发布。

**验收：**

- 设计、克隆、试听和新文本复用分别验证；preview_only 不可发布，明确确认和质量合格后才可发布。发布资格要求所有 required 检查通过，包含通过的 required `reusable_tts`；生产者还须核对固定测试集的必需项完整、名称唯一及结果证据，不能仅凭 schema 校验认定质量合格。
- 版本、材料摘要及绑定固定；普通退役禁止新引用，已固定任务在有效依赖租期内完成。

设计依据：[speech-api.md](architecture/speech-api.md)、[data-deletion.md](architecture/data-deletion.md)。

<a id="epic-341"></a>
## Epic 341 · 管理界面与基础工作台

为使用者和维护者提供可用、可恢复的文件语音、声音创作和基础管理体验。

**范围：** 管理端、公共组件、基础工作台、项目稿件、文件和配音界面、音色界面、共享采集播放及工作台内容生命周期。

**边界：** 语音底座实现及会议、字幕、助手、有声书、提词等复合业务由相应领域负责。

**领域接收条件：** 正式界面与同版公开服务联验任务、结果、失败、重放和恢复；工作台副本有明确所有权，删除及恢复先处理自身账本。采集播放取得用户明确授权，处理设备、页面与网络生命周期；基础功能可用性、无障碍及目标浏览器/设备有实际证据，界面如实呈现未配置或不可用。

<a id="u-project"></a>
### U-PROJECT · 项目与稿件版本

**需求：** 工作台项目和稿件版本、CAS、独立存储与恢复。

**验收：**

- 项目元数据使用 `metadata_revision` 作 CAS 前提；稿件 revision 不可变并可按固定版本读取。CAS 冲突、原键未知恢复和稿件历史读取一致；项目变更使用服务器返回值，身份/修订不符不报成功。当前项目契约只提供当前元数据读取，不能将修订号当作项目历史版本读取入口。
- 按同源生成客户端消费请求、响应及事件并验证 wire 默认值；fake 不注入真实遥测或标 ready。工作台不导入 Speech 内部、不共写底座表，维持共享交互语义。
- 独立schema/对象区、最小运行身份与迁移在真实部署可用；与底座恢复对齐独立副本、删除账本和恢复水位，完成重放后开放内容。
- 保留 v1 语义；需要模型绑定或纪要配置的消费者使用 v2。明确版本冲突与提交未知，不能自动换键。

设计依据：[technical-blueprint.md](architecture/technical-blueprint.md)、[data-deletion.md](architecture/data-deletion.md)。

<a id="u-admin"></a>
### U-ADMIN · 管理界面及任务中心

**需求：** 实际配置与运行事实、任务管理、SSE 恢复。

**验收：**

- 只显示实际模型、资格、就绪和预算，能力用ready/contract/planned，不以fake填补；诊断显式读取、无来源的数据保留未知，不能暗示身份隔离。
- Workspace创建/修改/配额及策略/音色、预设发布/回滚/激活/退役使用固定版本CAS及明确确认，冲突和拒绝结果可操作。
- 同版真实后端与浏览器联验底座服务；观测有界且不含业务内容。
- 部署状态按真实装配报告五项能力 available/disabled/unavailable；未配置明确 disabled，未知不伪装可用。
- 真实后端和浏览器验证任务分页、筛选、详情、进度、取消与重试；刷新、SSE断线/历史过期重读权威状态，按精确游标续读。
- 结果未知不显示成功、不自动换键或假定资源已释放；事件迟到和存储失败有明确处理及键盘确认。

设计依据：[technical-blueprint.md](architecture/technical-blueprint.md)、[data-deletion.md](architecture/data-deletion.md)。

<a id="u-file"></a>
### U-FILE · 基础文件转写与配音界面

**需求：** 上传、校对、字幕导出、试听、下载及断线恢复。

**验收：**

- 原始转写与追加校对修订分层、原文及历史保留；真实结果导入并按固定版本导出文本及冻结格式/语义的字幕时间轴，浏览器通过正式消费者读取，旧数据格式及失败边界保留。
- 同键重放、冲突、取消未知、刷新/断线、事件过期及迟到保持原身份与上下文；Range、Asset/Artifact过期依契约。
- 验证真实服务、隔离 PG 和生产浏览器，音频 ready 未交付前不标可用；Asset 上传未知时保留原请求和已提供的幂等键；已知原 Asset 身份时按 Location 查询；提供过幂等键时可使用原键和原请求协调。无键且无原身份时不保证恢复原意图；未知响应保持 X-Should-Retry:false。当前 Asset 上传上限默认且最高为 4 MiB，默认检查器仅检查文本；同步转写的应用层音频上限默认 25 MiB、可配置最高 64 MiB；当前公开路由另以 64 MiB 加 64 KiB 限制整个 multipart 请求体，包含表单字段和边界开销。两项限制独立，不能与 Asset 上限混同。
- 固定文本、voice/profile/model revision，真实TTS试听及下载，重试不重复发布。
- 验证Range、MP3/WAV/PCM和过期边界；取消未知、断线、迟到与原键恢复；浏览器不持引擎、数据库或组件凭据。
- CustomVoice 每 intent 最多 400 codepoints；指令 Nexus 入口上限 2000、标准请求及内部命令上限 4096、固定版本默认 pipeline 上限 500，其有效映射待确认并验证，不静默截断。

设计依据：[technical-blueprint.md](architecture/technical-blueprint.md)、[data-deletion.md](architecture/data-deletion.md)。

<a id="u-voice"></a>
### U-VOICE · 工作台声音创作界面

**需求：** 材料确认、候选试听、资格、发布和版本展示。

**验收：**

- 设计与克隆材料分别确认，材料变更需重新确认，两种真实模式分别取得资格。
- 候选、再生成、试听、换文复用和失败可观察；preview_only不可发布，复用资格通过且明确确认后方可发布不可变VoiceRevision并由真实TTS使用。
- 退役/禁用/删除及竞争与共享部署文案一致；真实来源、版本和资格不能以目录、草稿或合成页面代替。
- 单修订读取核对 `voice_id` / `revision`，按契约展示状态及退役时间；禁用时展示 `disabled_reason`。当前退役状态未提供原因字段，如需展示退役原因须先补齐契约；真实成功路径和两种生命周期分支分别验证。

设计依据：[technical-blueprint.md](architecture/technical-blueprint.md)、[data-deletion.md](architecture/data-deletion.md)。

<a id="u-media"></a>
### U-MEDIA · 浏览器采集与播放

**需求：** 可访问的设备权限、采集与播放控制；实时业务协议由实时服务提供。

**验收：**

- 真实双向流验证credit/gap/epoch、水位、文本冻结与连续PCM；采集播放和输出硬上界，PCM不进入React查询缓存。
- 旧generation/迟到音频不串播；停止、离开、异常或迟到授权释放本地设备/AudioContext；取消不假设远端停止。
- 批准HTTPS/WSS同源入口及支持浏览器、麦克风/系统音频矩阵；权限拒绝撤销、切换、后台休眠、断网重连、长时及明确gap实测。
- 完整键盘/焦点/小窗口/读屏及Minor处理，区分合成、物理设备、GPU证据；创建后焦点与取消路径生产bundle待复核。
- 浏览器在页面打开或会话未准入时不请求麦克风；延迟授权及用户主动移焦不得造成资源泄漏或抢焦点。

设计依据：[technical-blueprint.md](architecture/technical-blueprint.md)、[data-deletion.md](architecture/data-deletion.md)。

<a id="u-delete"></a>
### U-DELETE · 工作台内容所有权与删除恢复

**需求：** 工作台存储、预检、账本、独立副本及恢复。

**验收：**

- 版本化预检/确认、删除账本、对象擦除、运行 sweeper 和启动重放一致；清理身份未配置返回 `deletions_unavailable` 503 并拒绝受理。当前 MinIO 适配器只按前缀清理可见对象，不逐版本删除；对象阶段完成不能证明历史版本已物理擦除或前缀必定为空。
- 不误删独立副本；恢复时对象与依赖图须反映删除账本重放结果，重放完成后才开放内容查询、引用创建和执行入口，不承诺撤销已擦除内容。当前部分工作台后台任务在启动重放前已创建，尚无统一启动屏障；恢复时须隔离相关角色并验证顺序。
- 真实清理DSN、非owner角色和迁移，PG/对象存储下故障、未知PUT、迟到写入、重启和跨模块联验。

设计依据：[technical-blueprint.md](architecture/technical-blueprint.md)、[data-deletion.md](architecture/data-deletion.md)。

<a id="u-accept"></a>
### U-ACCEPT · 基础界面真实消费者与浏览器资格

**需求：** 覆盖真实页面和可访问性证据，禁止 fake 冒充生产。

**验收：**

- 正式消费者和待接操作可追溯到实际入口；测试调用不充当生产消费，接线数不等于验收率。
- 证据可复现并标明代码版本、环境、通过/失败/跳过或未运行；合成、真实模型、物理设备及目标拓扑分别列明。
- 实际页面按各功能Gate提供同版业务证据；设备、权限、断网重连、取消迟到、长时、跨浏览器、键盘焦点、小屏、读屏及减少动态完成支持矩阵，延后功能待自身阶段验收。
- 面板覆盖 360px、减少动态与读屏；历史入口页摘要不能替代完整交互资格。
- 管理、真实任务中心、文件转写、配音及共享采集播放各自覆盖正常、失败、原键恢复和同版浏览器验收；能力状态真实且共享部署不伪装权限隔离。

设计依据：[technical-blueprint.md](architecture/technical-blueprint.md)、[data-deletion.md](architecture/data-deletion.md)。

<a id="u-shared"></a>
### U-SHARED · 共享部署的基础体验与辅助功能

**需求：** 两个可选产品入口以实际能力展示状态，不虚构用户隔离。

**验收：**

- 无登录启动；工作空间、项目及音色明确共享，不展示成员、角色、租户或个人配额等未实现功能。
- 页面键盘、焦点、360px 小屏、读屏与减少动态符合声明浏览器矩阵。

设计依据：[technical-blueprint.md](architecture/technical-blueprint.md)、[data-deletion.md](architecture/data-deletion.md)。

<a id="epic-342"></a>
## Epic 342 · 工程、部署与运维

提供可安装、可维护且符合声明网络与拓扑边界的部署、恢复和发布能力。

**范围：** 构建交付、部署、拓扑、网络保护、观测、备份恢复、发布工具和运维交接。

**边界：** 业务语义、业务端到端接收及 SDK/MCP 能力由相应领域负责。

**领域接收条件：** 干净制品、独立服务入口、必要 worker 和运行身份按声明部署；受控迁移/回退、删除账本重放、备份恢复及明确 RPO/RTO 有实测证据。目标物理多机、备用 GPU、分区、节点故障、长时质量与容量及已启用业务链按实际拓扑验证；单机逻辑节点和 VM 不替代物理资格。部署网络、内容出站、日志与观测符合边界，许可、支持矩阵、安装和运维交接齐备，并按要求取得用户放行。

<a id="o-backup"></a>
### O-BACKUP · 生产迁移、备份与恢复

**需求：** 按目标拓扑冻结 RPO/RTO，迁移和灾难恢复先重放删除账本。

**验收：**

- 迁移/回退及唯一权威在目标拓扑实测，备份恢复满足预先冻结的 RPO/RTO；旧资产缺少存储声明时受控 adoption，升级先停止/隔离旧 writer。
- 恢复先重放删除账本才开放内容，unknown/隔离未清理不报告完成或返预算，高水位/token 不倒退；旧 NULL/损坏事实不补造时间和资格。
- 部署前冻结可验证的 RPO/RTO；初始评估可采用 RPO≤15 分钟、RTO≤60 分钟，须以目标拓扑恢复演练证实，不作为未测通用承诺。

设计依据：[deployment-units.md](architecture/deployment-units.md)、[data-deletion.md](architecture/data-deletion.md)。

<a id="o-role"></a>
### O-ROLE · 服务制品、worker 与职责隔离部署

**需求：** 版本化角色、工厂、配置、权限和可恢复进程启动。

**验收：**

- 声明的必要角色实际启动，语音 API/工作台不持有 Docker socket 或直接执行引擎；受管角色隔离权限，跨进程持久输入与制品经对象存储，实时内容经约定的网络流协议传递。
- ready/peak/steady/drain 和未知停止有真实来源；未取得可信停止与释放不退预算，故障恢复沿原身份。
- 版本化清单覆盖Speech API、Workbench API、后台命令、Gateway、Controller及未实现必要角色；列入口、身份、依赖、端点、权限、持久/临时区、健康就绪和平台；process_role不等于自动分派。
- 无Gateway/Controller包、GPU runtime和Docker socket的应用干净安装启动；媒体依赖按声明能力裁剪；缺配置/错误角色/缺依赖有定位明确的失败。
- 业务、数据、算力分机显式地址无localhost回退，最小凭据，运行与迁移身份分离且业务启动不执行DDL；关闭可选Workbench不影响Speech。

设计依据：[deployment-units.md](architecture/deployment-units.md)、[data-deletion.md](architecture/data-deletion.md)。

<a id="o-upstream"></a>
### O-UPSTREAM · Speech 与工作台独立部署与入口

**需求：** 独立上游、受控代理和禁止隐式本机回退。

**验收：**

- `/v1/workbench/` 和 `/v2/workbench/` 到 Workbench 工厂，其余语音 `/v1/` 路由到 Speech 工厂；上游不同机/端口，原路径、错误、流式、Host/Origin 不被 SPA 吞掉；跨机用批准的 TLS。当前仓库的 Web 代理将这些路径发往同一 `api:8000`，尚不能证明独立上游或跨机 TLS 已通过验收。
- Workbench停止不影响Speech，无法访问时明确失败、不误发或本地回退；用不同地址真实入口及隔离PG/对象存储验证工作台写入读取。
- LLM默认关闭；工作台只消费公开Speech协议，不共写表、不持引擎凭据或容器管理权限；实时owner路由由相关交付承接。

设计依据：[deployment-units.md](architecture/deployment-units.md)、[data-deletion.md](architecture/data-deletion.md)。

<a id="o-media"></a>
### O-MEDIA · Linux 媒体检查跨平台隔离

**需求：** 固定媒体检查部署边界与受控对象引用。

**验收：**

- 比较复用worker与新服务并记录所有者、权限、容量与回退，不因迁移平台自动新增常驻服务；业务不得导入/启动Linux监督器且隔离限额不降级。
- 固定检查/对象版本、摘要长度、限额、原身份、持久意图/回执、查询、取消和停止释放；跨机仅受控对象引用，禁任意路径/URL/命令/文件描述符。
- 同固定版本机器契约/生成消费者正反例及合成样例，覆盖外发前后崩溃、丢回执、同键异体、超限、取消竞争、节点/对象不可用；未知按原身份恢复，取消不等于释放，新增预算阶段同PG事务准入。
- 列Linux真实沙箱、跨机、公开音频ready后续验证；设计交付不代替相关交付。

设计依据：[deployment-units.md](architecture/deployment-units.md)、[data-deletion.md](architecture/data-deletion.md)。

<a id="o-portable"></a>
### O-PORTABLE · macOS/Linux 原生及联合平台资格

**需求：** 服务可独立安装和启动，能力矩阵与用户放行。

**验收：**

- macOS与Linux原生干净锁定安装、严格类型和依赖检查、实际工厂启动及适用PG/对象存储行为；至少独立Speech/Workbench API，Scheduler/Job等缺口明确。
- 应用不能导入启动Linux沙箱或安装GPU runtime；平台专属代码按部署边界隔离，Linux真沙箱和全仓严格检查保留，不用全局ignore/exclude或mypy --platform linux假装macOS通过。
- 任一必需检查失败汇总失败；同一集成版本、制品、Python/OS/架构、启用能力标注安装/导入/启动/行为及失败/跳过/not_run；不重复GPU/浏览器/全库任务。
- 保留现有 CI 覆盖及资源预算；Windows 等未验证平台明确标注，不自动下载模型或操作已有引擎。
- 汇集服务清单、独立上游、媒体边界、两平台必需检查，同版逐单元记录实现、通过、失败、跳过与not_run。
- Speech/Workbench API和通用调度任务不依赖本机GPU、Docker socket或Linux沙箱；PG完整配方原子事务和模块表所有权保持。
- 维护者确认且用户明确放行才关闭；CI 绿、子项关闭和 fake 不能自动放行。完整发布仍须满足原阶段前置、物理多机、备用 GPU、HA/RPO/RTO、可信停止释放、业务验收及最终用户放行，本资格不能替代这些关口。
- 业务/控制与 Linux 算力面跨物理主机验证公开文件 ASR/TTS 及已启用实时链，覆盖响应丢失、分区、重启、节点丢失和备用 GPU；逻辑集群或 VM 不代替物理资格。
- 独立 API 上游、同版服务清单、裁剪依赖及 macOS/Linux 原生运行作为部署前置，适用放行点由用户明确决定。

设计依据：[deployment-units.md](architecture/deployment-units.md)、[data-deletion.md](architecture/data-deletion.md)。

<a id="o-physical"></a>
### O-PHYSICAL · 目标物理拓扑与故障演练

**需求：** 多主机接管、隔离、长时与备份资格。

**验收：**

- 目标拓扑实测真实 stop、旧入口封锁、进程 kill、网络分区/节点丢失及长时资源曲线，负载不超预算。
- 备用 GPU/物理多主机接管独立验证；若缺第二主机明确条件缺口，不以单机模拟宣布通过。

设计依据：[deployment-units.md](architecture/deployment-units.md)、[data-deletion.md](architecture/data-deletion.md)。

<a id="o-release"></a>
### O-RELEASE · 许可、运维、发布与交接

**需求：** 平台文档、许可、运行环境和部署交付证据。

**验收：**

- 发布材料逐项列明受支持平台、实际运行角色、配置/健康就绪、操作、备份和回退；仓库 LICENSE 须经用户决定，模型/依赖/素材许可证据齐备。
- 生产 Linux 镜像、目标拓扑、清理身份/迁移和恢复交接完成部署验证；出站监测及共享部署配置证据归档。
- 用户文档、运维文档及未验证项完整；浏览器/无障碍结果引用业务与界面负责人提供的证据，不以摘要、fake 或单次 CI 宣称完整资格。
- 将部署版本、许可、运行及恢复材料、已验证和未验证项以及部署前置交给总控；部署证据不单独构成完整业务放行。

设计依据：[deployment-units.md](architecture/deployment-units.md)、[data-deletion.md](architecture/data-deletion.md)。

<a id="o-boundary"></a>
### O-BOUNDARY · 部署入口与内容出站边界

**需求：** 部署层负责 TLS、Host/Origin、可选共享服务凭据及内容不出网。

**验收：**

- 浏览器同源，写入与 WS 校验 Host/Origin；SDK 缺 Origin 依显式入口规则，不用 Origin 冒充身份。当前相关中间件只覆盖部分资源入口，不能推断同步语音或未装配的实时 WS 已受统一保护。
- 管理页面与 API 同时受部署路由控制；凭据不暴露浏览器、日志或公开 DTO；语音内容不离开部署网络，包括代理和观测链。当前公开语音处理器不验证共享服务密钥，依靠代理或网关保护时须单独配置并验证，不能将 `Authorization` 头当作已生效的认证。

设计依据：[deployment-units.md](architecture/deployment-units.md)、[data-deletion.md](architecture/data-deletion.md)。

<a id="o-observe"></a>
### O-OBSERVE · 可观测、质量和发布证据

**需求：** 按版本、环境、拓扑与能力分离运行状态和资格证据。

**验收：**

- 相关性串联 request/operation/job/attempt/epoch；日志不含音频、正文、特征、完整 prompt、凭据及私有路径，ID 不作高基数指标标签。
- 报告排队、准入、推理、取消、quarantine、内存、缓冲、重复发布及客户端采集/网络/播放；fake、真实引擎、浏览器、物理多机证据分别列出。
- 按固定 binding、语料、负载和时钟采集并报告字幕显示、ASR 正文 final 和常规控制 API 等指标，区分候选目标和实测结果；观测报告不代替各业务域的服务验收。

设计依据：[deployment-units.md](architecture/deployment-units.md)、[data-deletion.md](architecture/data-deletion.md)。

<a id="o-build"></a>
### O-BUILD · 构建依赖、镜像与可移植制品

**需求：** 按角色固定依赖与部署制品，保留 CI 必需门禁。

**验收：**

- 提交镜像和依赖锁、模型摘要、环境 schema、数据库迁移及回滚说明，不提交凭据、真实音频或模型权重。
- 业务 API 无 GPU、Docker socket、Linux 沙箱强依赖；应用、引擎与节点平台职责分离，单机采用与集群一致的协议与恢复路径。

设计依据：[deployment-units.md](architecture/deployment-units.md)、[data-deletion.md](architecture/data-deletion.md)。

<a id="epic-343"></a>
## Epic 343 · 复合场景与内网智能助手

在语音和工作台基础能力之上，交付会议、助手、有声书及实时辅助场景。

**范围：** 实时字幕、会议和纪要、语音助手、提词、有声书及受控内网 LLM 业务编排和导出。

**边界：** 通用语音、执行、基础工作台及共享媒体组件由相应领域负责。

**领域接收条件：** 会议记录可编辑、版本化、封存及导出；助手和纪要在批准的内网服务下可靠处理失败、取消与迟到结果，插话不播放旧代音频。有声书支持固定版本、局部重做、独立副本、质量确认与完整导出；字幕和提词在实际设备及恢复场景中工作。LLM 默认关闭，仅服务端批准本机/内网端点，不将内容回退公有云。

复合业务完成各自验收后汇集联合证据供用户接收；按既定排期推进，共享前置由各自主责继续。

<a id="w-caption"></a>
### W-CAPTION · 实时字幕场景

**需求：** 复用共享采集与实时流的字幕场景及可访问性。

**验收：**

- 与共享实时音频同版验证真实字幕、gap和生命周期，完整无障碍及浏览器资格；与合成和无麦克风的拒绝态证据区分。
- 未连接或会话创建被拒时不请求麦克风；仅在服务准入后由用户明确启动采集。
- 字幕显示延迟按可对齐采样时钟的有效字词口径测量；P95≤1500ms 是 PRD 候选目标，须先按目标部署与负载冻结，不得表述为已经达标。

设计依据：[technical-blueprint.md](architecture/technical-blueprint.md)、[workbench-export-integrity.md](components/workbench-export-integrity.md)。

<a id="w-meeting"></a>
### W-MEETING · 会议保存、封存与导出

**需求：** 版本化转写、辅助关联、要点与导出。

**验收：**

- 保留session、gap、preview/final与辅助revision对应来源；封存原文不被校对改写，要点关联对应版本并按固定版本导出。
- 真实采集、匹配正文分人/对齐及并发/故障联合验证；未保存音频不得承诺会后重新识别。
- 实际同版路由和正式消费者支持 POST `/v1/workbench/projects/{project_id}/meetings`（createMeeting）及 POST `/v1/workbench/meetings/{meeting_id}/seal`（sealMeeting），分别核对请求、声明的成功/错误响应、持久身份和封存版本。
- 普通会议不依赖 LLM；会议资料封存/校对及内容生命周期在同版组合中可追溯。

设计依据：[technical-blueprint.md](architecture/technical-blueprint.md)、[workbench-export-integrity.md](components/workbench-export-integrity.md)。

<a id="w-prompt"></a>
### W-PROMPT · 持久提词与真实语音跟随

**需求：** 稿件 revision、有限匹配、手动接管与恢复。

**验收：**

- 固定稿件revision、有限跟随窗口；低置信暂缓，旧稿或旧epoch不推进；真实声音跟随、键盘与手动接管可验证。 当前页面提供手动阅读与状态保存，自动语音跟随尚未接入；匹配组件或合成测试不代表真实声音跟随已验收。
- 404 不假定位置 0，CAS 冲突不自动重试；普通提词不依赖 LLM，手动合成证据不代替真实跟随。手动接管和固定稿件版本包含在最终接收范围。

设计依据：[technical-blueprint.md](architecture/technical-blueprint.md)、[workbench-export-integrity.md](components/workbench-export-integrity.md)。

<a id="w-minutes"></a>
### W-MINUTES · 受控内网纪要

**需求：** 固定 sealed 内容及服务端批准模型生成纪要。

**验收：**

- 默认关闭，仅服务端批准本机/内网端点，不回退公有云。关闭时拒绝新的纪要意图且不新增纪要任务或回执；已接受意图按原键优先重放，不因当前关闭而重新派发。当前存储在事务内检查原回执后再检查配置，不能描述为事务前拒绝。
- 固定sealed转写、模板/模型/绑定版本；受控派发、持久原意图和恢复；失败不改成功转写。
- 独立保存导出、实际质量、浏览器/代理/观测出站检查及同设备父预算和混合负载验证。
- 实际同版路由和正式消费者支持 POST `/v1/workbench/meetings/{meeting_id}/minutes`（createMeetingMinutes），核对请求、202 和契约声明的错误、结果版本与原身份恢复。

设计依据：[technical-blueprint.md](architecture/technical-blueprint.md)、[workbench-export-integrity.md](components/workbench-export-integrity.md)。

<a id="w-assist"></a>
### W-ASSIST · 受控助手轮次与插话

**需求：** 内网 LLM、语音任务和 generation 之间的真实闭环。

**验收：**

- 批准内网LLM POST返回202后GET succeeded，output_text非空，speech_job_id可真实查询且属于同turn；voice已发布并固定revision和资格。
- queued/running/succeeded/failed/cancelled语义；同键重放、丢回执/重启不重复创建Job，同键异体409；HTTP 200/202取消与OpenAPI一致，原身份查询恢复，未获证不宣称停止释放。当前助手路由对活跃轮次返回200、已终态返回202，与OpenAPI的状态码描述相反，尚须修复并验收。
- 插话先停旧播放，文本/TTS/播放器generation关联，迟到音频隔离；超时取消后远端stop/release对账。
- 默认关闭时不新增轮次、无公有云回退；当前创建入口先检查启用状态，关闭时已接受轮次的原键POST也被拒绝，仍可按原turn ID查询。真实HTTP/隔离PG/生产浏览器、失败注入、质量和内容出站、父预算/混合负载证据独立于fake。
- 能力为 disabled 或 unavailable 时不提供创建入口；failed 必有错误，非 failed 不得伪造错误，succeeded 必有关联真实 Job 和非空文本。

设计依据：[technical-blueprint.md](architecture/technical-blueprint.md)、[workbench-export-integrity.md](components/workbench-export-integrity.md)。

<a id="w-book-unit"></a>
### W-BOOK-UNIT · 有声书固定版本与局部生成

**需求：** 有界原意图、版本依赖、并发与局部重做。

**验收：**

- render_unit固定稿件/章节/角色/voice/词典/model/profile/规划版本和UTF-8指纹；CAS与持久有界历史关联，改文只影响目标单元、换声只影响同角色。
- 外发前持久化原意图，并在一致性边界核对固定 revision；同键异体冲突，重放返回原回执，未知按原身份恢复，旧/错 unit、Job、代次或指纹拒绝。当前生成路径先提交底座 Job，再记录工作台生成事实，尚未满足此前置要求。
- 生成历史分页及恢复扫描保持有界，待处理项不得因固定的最老批次长期饥饿；可恢复失败有持久退避，区分来源不可用与永久失效。页面拒绝过期异步结果；以隔离 PG、可控 producer 故障及并发验证。
- 固定声音修订读取区分不存在和服务不可用，失败不得展示陈旧记录；延迟响应后不抢回使用者已主动移走的焦点。以重复及真实浏览器交互验证，并覆盖键盘与 360px 窄屏。
- 实际同版路由和正式消费者支持 POST `/v2/workbench/render-units/{unit_id}/generations`（`generateRenderUnitV2`），核对请求、202 和契约声明的错误、固定版本及原键语义。v1 的 `generateRenderUnit` 仍在公开契约中，但当前路由和页面未接入；须决定并交付其支持或退役，不能用 v2 接线冒充 v1 已覆盖。

设计依据：[technical-blueprint.md](architecture/technical-blueprint.md)、[workbench-export-integrity.md](components/workbench-export-integrity.md)。

<a id="w-book-export"></a>
### W-BOOK-EXPORT · 有声书独立副本及导出

**需求：** 独立资源、冻结清单、staging、恢复和可下载制品。

**验收：**

- 真实底座结果和独立副本按摘要及非空source receipt校验，公开非JSON下载、Range/416与到期状态正确；领域ready不代替实际可导出。
- worker不调用推理引擎，CPU/内存/磁盘/并发有界并计入预算；固定清单和检查点，staging校验后原子发布，取消/重试/续作不重复发布。
- 识别缺章/空成品/部分失败；质量检查、试听和匿名确认分别保存独立记录，关联固定内容版本与导出清单，不能用单一综合成功状态或局部可用替代场景验收；独立副本与完整导出保留各自生命周期。工作台关闭不影响底座，此项独立于助手。
- 保存请求键跨刷新保留，确认后读取失败不得误报未知；真实副本/导出和恢复取证区分合成fixture。
- 导出创建的 `Idempotency-Key` 为可选；当前事务先比较 `expected_casting_revision` 再查询原键回执，配音版本改变后重放已接受意图也会返回版本冲突。须补齐并验证原身份恢复，结果未知时不得自动换键创建新意图。
- 正式消费者实际接入 GET `/v1/workbench/saved-assets/{asset_id}/content`（downloadSavedAsset）、GET `/v1/workbench/exports/{export_id}/content`（downloadBookExport）和 GET `/v1/workbench/exports/{export_id}/events`（streamExportEvents）；核对二进制媒体类型、Range/416、事件时序、断线恢复、取消和过期，不以合成 TCP 测试代替真实工作台联验。当前下载路由和客户端已有接线；事件流尚未在生产工作台路由和正式页面接入，契约或生成类型不构成流式恢复证据。

设计依据：[technical-blueprint.md](architecture/technical-blueprint.md)、[workbench-export-integrity.md](components/workbench-export-integrity.md)。

<a id="w-llm"></a>
### W-LLM · 默认关闭的内网 LLM 边界

**需求：** 助手/纪要使用服务器批准的本机或内网 LLM，底座可独立运行。

**验收：**

- 默认关闭、失败不回退公有云；LLM 端点不可由客户端任意指定，内容、浏览器代理与观测不出网。
- 固定输入、模型和模板 revision；LLM 失败不改写已成功转写或重跑底座任务；共享物理算力计入预算。

设计依据：[technical-blueprint.md](architecture/technical-blueprint.md)、[workbench-export-integrity.md](components/workbench-export-integrity.md)。

<a id="epic-344"></a>
## Epic 344 · 通用契约、SDK 与 MCP

让不同语言的正式消费者依据同一公开契约可靠调用、解释结果并恢复错误。

**范围：** 跨域机器契约、生成与兼容工具、通用 SDK 和无状态 MCP 基础。

**边界：** 具体业务操作语义、路由及业务端到端接收由各业务领域负责。

**领域接收条件：** Python/TypeScript 消费者与生产者同源生成或校验，覆盖兼容、错误、幂等、取消及事件语义；声明的 OpenAI 兼容子集与扩展有明确边界。SDK 可安装、配置并运行调用与恢复样例；MCP 可发现、调用，并与对应 REST 命令及错误语义一致，真实消费者取得同版结果。

<a id="c-mcp"></a>
### C-MCP · 无状态 MCP 客户端接入

**需求：** 基于公开语音资源的无状态工具、发现与调用。

**验收：**

- MCP 与 REST/WS 同命令参数域、默认值、错误、幂等和取消，MCP 无独立默认值、不持有模型或 WS 会话、不绕过准入。 当前契约定义了无状态投影；已核对的语音 API 组合根未注册 MCP 工具入口，正式工具及消费者尚需接入和验收。

设计依据：[README.md](../contracts/README.md)、[contract-ownership.md](architecture/contract-ownership.md)。

<a id="c-sdk"></a>
### C-SDK · 生成 SDK 的安装和恢复示例

**需求：** 声明平台上的 SDK/MCP 安装、发现、调用与错误恢复。

**验收：**

- SDK/MCP 安装、发现、调用、错误恢复示例在声明平台可运行；模型、镜像、依赖、素材许可及仓库 LICENSE 决策可追溯。

设计依据：[README.md](../contracts/README.md)、[contract-ownership.md](architecture/contract-ownership.md)。

<a id="c-contract"></a>
### C-CONTRACT · 同源机器契约与兼容消费者

**需求：** 公共、执行、部署控制的机器契约、生成器、兼容与类型一致。

**验收：**

- REST/WS/MCP 同一命令的参数域、默认、错误、幂等和取消语义一致；Python/TS 同源生成、运行时正负例及重生成无漂移。
- 不维护手工并行语义副本；domain 不依赖生成 DTO；不扩大 OpenAI 兼容承诺；公开文档明确标准 Audio/Realtime 子集和 nexus 扩展。
- 对 `contracts/workbench-openapi.yaml` 的 createMeeting、sealMeeting、createMeetingMinutes、generateRenderUnit、downloadSavedAsset、downloadBookExport 和 streamExportEvents，验证同源 HTTP 边界对请求、声明的响应、错误与媒体类型的校验；真实业务路由及消费由各业务需求负责。当前基础类型化客户端不自动校验响应，`workbench-http` 另提供显式校验入口和受校验客户端，使用方须选择并正确接入。
- Python 与 TypeScript 实际消费者对严格类型、额外字段、联合类型、跨字段约束和错误状态使用同组正负例并作一致判定。

设计依据：[README.md](../contracts/README.md)、[contract-ownership.md](architecture/contract-ownership.md)。

<a id="epic-345"></a>
## Epic 345 · 场景预设与有效配置解析

使请求使用明确、可重放的场景配置与参数，避免执行时读取变化中的默认值。

**范围：** 治理配置、场景预设、版本发布、profile、overrides 与 effective configuration 解析。

**边界：** 模型 binding 生命周期、发音资源和实际执行由相应领域负责。

**领域接收条件：** 预设与策略按契约解析默认、覆盖、allowlist 和范围；不支持或越界参数在读取对象和外发前拒绝。发布、激活、CAS、回滚及固定读取有明确结果，原身份重放不受后续默认变化影响；实际消费者与执行命令使用同一有效版本。

<a id="p-profiles"></a>
### P-PROFILES · 场景预设和有效参数解析

**需求：** 不可变 profile、参数域、允许 overrides 和 effective 配置。

**验收：**

- Profile 草稿和已发布 revision、资格与默认指针分明；按预设 head 的版本前提事务内 CAS 发布/激活，回滚不修改既有 Job。当前仅有草稿分配与固定 revision 读取，发布、激活及公共列表仍待交付，详见[Profile 草稿](components/profile-drafts.md)。
- 绑定默认值→已发布 profile→允许 overrides；工作空间/部署治理是最终硬约束，越界拒绝，不伪造有效值。
- require_exact_parameters 下任何自动收紧返回 422；非 exact 返回实际值与原因。
- 参数字典覆盖 ASR preview_interval_ms 100–5000、max_segment_ms 1000–30000 且不小于 preview、final/auxiliary deadline 100–10000（预设中的 auxiliary 字段必填，schema 的 5000 是默认注解，不自动填充）、rollback_tokens 0–32、VAD 静音 200–3000；TTS planner max_wait_ms 100–3000。绑定不支持时拒绝所需覆盖；各字段分别在范围内不代表 max_segment 与 preview 的跨字段约束已满足。
- 给定 workspace 分组与逻辑模型/任务，只读解析已发布配置，返回固定 profile/binding revision、采样率、语言、限额及端点能力；未发布或不存在明确 unavailable/not_found，不加载模型、猜测或回退草稿。
- Job `effective_configuration` 与执行命令的 profile/binding revision 一致，发布或默认值变化后原任务不漂移；隔离 PostgreSQL 验证，不增加跨库双写或进程内全局锁。
- 预设发布先校验资格；expected_current_revision 在预设当前 head 锁及同一事务内比较，不用 MAX 聚合代替 CAS；并发激活仅一个成功，退役预设不进入默认解析。

设计依据：[speech-api.md](architecture/speech-api.md)、[technical-blueprint.md](architecture/technical-blueprint.md)。

<a id="p-policy"></a>
### P-POLICY · 策略、资源配置与请求覆盖隔离

**需求：** 预设、业务流程、模型绑定和治理策略明确分离。

**验收：**

- 推理 overrides 不控制调度优先级、池预算、引擎地址、模型路径或部署策略；不支持的参数明确拒绝。
- profile revision 固定 schema、required capabilities、allowlist/ranges、SLO 和验证；CAS 发布和回滚只切指针。
- 治理 policy 的版本或状态变更与通知 outbox 同一事务提交；执行域消费已提交的固定 revision，策略刷新不代替权威准入/发布裁决。

设计依据：[speech-api.md](architecture/speech-api.md)、[technical-blueprint.md](architecture/technical-blueprint.md)。

<a id="epic-346"></a>
## Epic 346 · 模型绑定、资格与目录

让部署使用已核验来源、能力和运行资格的固定模型绑定，并向消费者如实公开支持范围。

**范围：** 模型和引擎来源、制品、binding revision、能力资格及目录投影。

**边界：** 实际实例启停、执行结算及声音资源发布由相应领域负责。

**领域接收条件：** 模型、依赖、许可、实际参数及输入输出上界可追溯；binding 的注册、发布、激活、退役与禁用按版本裁决。目录准确区分可用、禁用和不可用，不从缺失证据推断就绪。逐能力完成质量、容量、加载、status、abort 和停止资格，真实模型、语料、混合负载及目标部署有对应证据。

<a id="m-binding"></a>
### M-BINDING · 模型来源、绑定和支持矩阵

**需求：** 固定模型/引擎/镜像/许可、发布绑定和可声明能力。

**验收：**

- 文件、实时、各声音模式分别给可行/experimental/阻塞结论；每项可用声明有 request ID、status/abort、强输出上界、加载峰值与真实停止/释放证据。
- GPU 实验按批准制品和合成或授权语料排队，清理可核对并保护当期明确保留的共享服务；资格按固定硬件、版本和环境而非硬件名称推定。

设计依据：[capacity-manifest.md](operations/capacity-manifest.md)、[file-tts-binding-handoff.md](operations/file-tts-binding-handoff.md)。

<a id="m-qualify"></a>
### M-QUALIFY · 逐绑定质量与容量资格

**需求：** 按固定版本、拓扑、语料和负载取得真实质量/容量资格。

**验收：**

- 每项质量/容量结论绑定模型/镜像/权重、版本、拓扑、语料、负载、时钟和阈值，仅声明实际测量范围；镜像/许可/manifest 与告警可追溯，遥测不含业务内容。

设计依据：[capacity-manifest.md](operations/capacity-manifest.md)、[file-tts-binding-handoff.md](operations/file-tts-binding-handoff.md)。

<a id="m-disable"></a>
### M-DISABLE · 模型绑定目录生命周期

**需求：** 不可变发布事实、退役/禁用与激活 CAS；实际停止由执行域接收。

**验收：**

- 增量迁移；如变更冻结的 binding 激活路由，先完成契约流程；ADR 0013 为 proposed。
- 保留已发布事实行。发布、激活和退役 HTTP 操作已载于 admin 契约；新增或变更其路由须单独完成契约流程，契约存在不代表管理 UI 或完整生命周期验收已完成。
- retire 保留精确历史且禁止新引用；目录 revision 随生命周期变化、ETag 改变；激活在当前 head 锁与同事务比较，回滚不修改已固定任务。
- 绑定退役/禁用的资源状态、revision 增长与通知 outbox 在同一事务提交；运行态拦截、取消和释放由执行域依固定 revision 事件处理。

设计依据：[capacity-manifest.md](operations/capacity-manifest.md)、[file-tts-binding-handoff.md](operations/file-tts-binding-handoff.md)。

<a id="m-catalog"></a>
### M-CATALOG · 发布目录与模型能力如实投影

**需求：** 已发布 binding、配置、readiness 与资格来源和缓存版本。

**验收：**

- 按 workspace 分组和模型解析发布事实；`configured`、`engine_ready`、`quality_qualified` 三个布尔字段和 `admission_state` 分别有可信来源。缺少相应就绪或资格证据时保守投影为 `false`，无法证明可准入时 `admission_state` 为 `unavailable`；这些投影本身不证明实际引擎故障或资格失效。当前能力接口仅校验 workspace 标识格式，不验证其存在或据此过滤目录；默认目录也未接入实时 readiness 证据，不能用响应证明该工作空间已获准入。
- 目录、模型详情和能力快照遵守 ETag/304；生命周期改变目录 revision；绑定与实例 generation/声音版本分开建模。 当前这三个路由的 200/304 响应同时发送 `Cache-Control: no-store`；ETag 支持条件请求，不表示允许缓存保存响应。
- 目录、单模型及能力端点遵守契约：目录或 policy revision 变化使 ETag 变化，If-None-Match 命中 304；同 revision 的规范摘要不含请求时间并字节稳定，公开音频字节、文本码点、分段、实时会话和排队等待上界。
- 针对当前已发布 revision 的实际数据形状执行回归，并在真实部署以请求证明 `/v1/models` 返回 200；历史 503 不推定仍是当前故障，缺事实不能伪装 ready。

设计依据：[capacity-manifest.md](operations/capacity-manifest.md)、[file-tts-binding-handoff.md](operations/file-tts-binding-handoff.md)。

<a id="m-registration"></a>
### M-REGISTRATION · 资格化发布与运行版本切换

**需求：** 按绑定固定模型/镜像及资格；运行实例由执行域负责。

**验收：**

- 生产发布绑定引用固定 manifest/qualification 与质量报告；实验性、不完整报告不得当生产资格。 当前 HTTP 草稿保存的定义不含 `binding_id`，目录投影要求该字段；通过该路径发布不代表目录可读取，缺口及修复后的正式联验须单独验收。
- 模型升级为新 binding revision；新请求切换，旧会话 drain 与旧 Job 按固定版本完成或明确取消，回滚不改历史制品。

设计依据：[capacity-manifest.md](operations/capacity-manifest.md)、[file-tts-binding-handoff.md](operations/file-tts-binding-handoff.md)。
