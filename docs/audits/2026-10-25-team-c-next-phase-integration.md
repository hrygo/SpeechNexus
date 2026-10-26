# 团队 C 下一阶段：共同基线集成与验收证据

核验日期：2026-10-25–26，Asia/Shanghai。依据：[阶段 Goal 与验收标准](../plans/2026-10-25-team-c-next-phase-goal-and-acceptance.md)，version 0.2.0。

结论：PR #153 已完成共同基线集成，原6项HTTP兼容性发现已有显式版本/消费者方案并通过合前门禁。**M1仍未通过，M2/M3未验收**；Profile草稿消费者[PR #165](https://github.com/hrygo/SpeechNexus/pull/165)已于2026-10-25 22:13:48合入main `639b703`，14项合前CI全部成功；验收映射继续保留在原工作树。C02有效TTS修订和ASR导入/固定TXT消费者分别由#167/#170后续集成；仍为局部交付。下半部17:02的失败与修复记录保留为历史事实，不再作为当前HTTP阻塞。

## 当前共同基线与证据边界

| 对象 | 已核实事实 |
| --- | --- |
| 固定base / PR head | `58bc51b3e592eadfcc11fdac948bb2d2b6ad4ff4` / `f89862248e35c5248f8f07e0b8942c9e216f40af` |
| 实际合并 | [PR #153](https://github.com/hrygo/SpeechNexus/pull/153)，19:38:25合入main `70ab43ddf187b8928cd6864f5470541a077b5e89` |
| 合前CI | [40189168627](https://github.com/hrygo/SpeechNexus/actions/runs/40189168627)，14项全部成功；Python6033 passed、184 subtests passed、覆盖率85.23%；六个数据库/对象区/浏览器分片完整集合1636项，1614通过、22为既有PG-only适配器的InnoDB不适用项 |
| 测试树与合并树 | CI测试合并提交 `2c53879` 与实际merge均为 tree `c5d29b7f95ab4364d3693b38dceaa7a2f4949bd5`；不能将合前检查称为合后CI成功 |
| 有限浏览器/持久化证据 | 末轮隔离PG18.6/MinIO/生产浏览器三模块23 passed，覆盖章节创建、15项promoted探针及导出模块；PG与MinIO已停止。媒体是合成内容，不是物理设备资格 |
| 明确保留的资格 | 3项真实内网LLM测试独立为qualification，本轮未运行；真实语音、公开结果生产、受控迁移、物理设备/目标拓扑与完整AC继续待验 |

集成期间保留v1源语义，要求固定模型绑定及纪要配置的消费者显式使用v2；开发、部署和真实浏览器代理均升级。完整拥有资源快照、对象写入原意图/token屏障、未知写入阻断删除及确认PUT后的取消已集成。分片暴露的独立bucket前置、Admin刷新边界、PCM接收屏障及迟到章节列表竞态均已修复且保留原断言；具体改动/审查/证据见PR。schema revision11仅是新建隔离基线，A07仍需受控生产迁移。

## 当前T02映射审计

独立交付：Profile草稿切片基于main `2fe90fed`，PR #165 head `52ee02d`。正式Admin页面消费既有Profile写入/固定读取，先持久完整意图再发送，未知仅原键恢复，不以固定GET或入口拒绝消除未知；只提供草稿，未交付发布/激活/退役。独立审查发现回执预算、可选字段注入和刷新丢拒绝原因三项P2，新增5项反例先失败后修复并复审；另保护请求/响应独立上界。最终Admin151项、52项可访问性和真实A HTTP/最小角色/隔离PG18.6/Chromium验证通过，测试PG确认停止。合前CI [40200592906](https://github.com/hrygo/SpeechNexus/actions/runs/40200592906)对应head `52ee02d`，14项全部成功；2026-10-25 22:13:48合入main `639b703`，CI测试提交 `3e42b6f`与实际合并提交均为tree `1e5607da5ada0d81e67b5b770118246982b153f5`，不称合后CI。#36仅同步草稿进展、全部AC保持未验收。旧基线映射的源码引用与证据身份未自动改挂新提交，仍须逐项复核。

[可追踪映射](2026-10-25-team-c-acceptance-map.json)保存原41条AC及278个稳定语义子条件；历史38项按原摘要迁入后，已逐项补充和复核其余条件。最后26项覆盖C00事件/开发演练与C07结果/副本/导出/恢复/独立验收，当前全部278项都有候选映射、未审计数为0，所有子条件仍为not_run。候选映射不是完成证明，T02的实际角色、行为断言与充分证据仍待补。未把新head写到旧证据上，也未自动更新失效源码摘要。

[守卫](../../scripts/dev-env/check-acceptance-map.py)和反例覆盖范围/条件删除、缺消费者/测试、失效锚点、证据篡改、具名依赖缺解除条件、错误资格维度、旧head、失败/跳过/未知冒充通过及报告覆盖输入。独立审查发现旧证据可重挂已改源码、双份材料同步缩减范围、无效输入下报告覆盖证据三个P2；新增10个反例先失败，再通过独立原范围pin、运行源码清单校验、完整输入保护及hardlink识别修复。守卫41项及相关回归合计49项通过，均为本次未提交守卫的本地测试，未纳入PR #153合前CI。

源码复核单元直接读取6个变更文件的指定函数/断言，确认其字节与已测试的main `70ab43d`一致；22个子条件的41处引用已逐项复核后更新，旧摘要和理由保存在mapping_reviews。新增v2显式绑定、未支持参数零副作用拒绝与真实HTTP/PG CAS的行为测试映射，但未新增验收通过声明。当前图谱无此项目索引，结论限定直接读取的路径，不声称全局调用图完备。

最新严格审计仍退出1：126个映射角色缺项，证据格式错误为0。2026-10-26回读历史export 19项通过日志及隔离服务停止/移除元数据、Admin 145项合成日志及历史源码清单后，仅补两份附属文件的SHA-256；原文件、运行版本、源码引用和全部条件状态保持不变，未重跑或升级资格。补摘要前的2项错误保留在旧审计报告；复核记录为 `t02-historical-companion-digest-review.json`，新报告为 `acceptance-map-pr177-companion-digests.json`，均在 `.runtime/team-c/next-phase/2026-10-25T155546+0800/`。不能以校验器自身测试通过或已登记引用有效替代映射完成、原语义覆盖或AC通过。

C00/AC2新增8项映射核对了不可变revision、CAS、原键恢复、固定稿件历史及生产schema/对象区/最小角色装配。原CI中的版本和保存副本测试按准确node提取，保留实际checkout `2c53879`；提取不是重跑。公开项目历史的完整消费范围仍待核对，不将当前head查询或历史回执重放扩成完整历史入口。目标Linux镜像、受控迁移、对象区和角色资格分别记录A07/B07制品及解除条件。

C00/AC3–AC4新增9项映射区分以下事实：

- `uv run lint-imports` 当前扫描380文件、1089依赖，7条契约成立；导入边界反例8项通过。此证据不能替代目标workbench身份对speech表无写权限的负例。
- Token门禁及39项Node反例通过，保护同源Token/值/引用闭包。共享AppShell不等于全业务确认、冲突、键盘和辅助技术语义一致。
- 删除runtime在开放请求前重放账本，对象阶段失败保持未完成并拒绝启动；隔离PG/MinIO测试覆盖其他项目副本保留。9个相关原CI节点已核对为passed；不结算A07目标备份水位、跨模块源删除及恢复资格。
- 导出正式路由只注册create/get/cancel，BooksHost挂载的面板明确没有下载链接；worker默认无producer。zip/wav/mp3真实字节及冻结的公开下载/Range链路是C需补齐的实现与联验范围，不能用已排队、状态轮询或私有文本导出结算。

C01/AC1新增5项映射逐行核对了正式诊断Host、同源生成校验客户端、状态映射和测试。诊断实际只展示内网LLM配置模型/模板及工作台依赖配置投影，没有完整语音模型实例、资格报告或资源预算展示。预算/租约/GPU明确为空，属于待接入功能，不能因为没有造假而结算“展示实际事实”。冻结的listModelBindings/listModelInstances/getQualificationReport/listResourcePools仍须核验生产制品后逐项消费；本轮未判断这些上游接口均缺失，也未将整包设为阻塞。5个相关原CI节点均passed，其中两个为Chromium+C HTTP+PG，其余为能力配置投影；配置的合成不可达引擎/LLM端点不构成真实模型资格。

C01/AC3–AC4的13项任务中心映射已直接核对正式Host/客户端/会话/页面/恢复日志及浏览器断言。确认以下边界：

- TaskDetail尚未展示冻结Job.progress；TaskApi和页面没有retryJob，也未接status/kind/workspace_id列表筛选。这3项保留空test_refs与所缺行为断言，不拿取消重放或dev场景切换充当重试/筛选测试。
- 取消意图先持久原键，接受/终态/cleanup分开呈现；刷新不恢复Job快照，只恢复ID/原键/事件水位并要求显式重读。事件到达后完整GET才推进水位；410先持久清水位再GET，失败不称恢复。通用断线提示显式重读，现有浏览器探针尚未完成“断线后点击重读→新快照”操作断言；原AC未要求后台自动读取。
- 三项任务Chromium原CI节点均passed，但分别使用dev内存或production bundle合成回环HTTP/SSE，无持久speech后端。精确checkout与范围已保留，未称为本轮重跑或真实服务资格。
- 对固定`70ab43d`的speech create_app构造OpenAPI并发出5个本地ASGI请求：没有公开Job路径，列表/详情/事件/取消/重试均404。首次缺配置的构造未成功；补齐显式合成配置后才取得此结果。全程禁用写入runtime、不执行lifespan、禁止socket连接，未启动服务或访问数据库。这只证明该基线factory配置，不声称所有后续远端/部署均缺失。A04需交付具名完整持久Job/进度/事件/取消/重试制品，A01协调JobFact不能替代；T07逐操作核验后由C接正式消费者，不阻塞C先补进度、重试、筛选和已有恢复测试。

C02/AC1的6项原稿/校对/导出映射已区分实现与正式链路：domain对语音来源创建sealed原稿、校对追加draft并保留source_revision；真实PG与合成公开HTTP源证明这部分事务/历史行为。真实C HTTP/PG/Chromium校对探针使用手工合成draft，历史报告名sealedSource不改变事实，不能作为ASR封存或模型证据。后端PublicTranscriptSource只接固定Artifact/receipt的text/plain UTF-8并校验身份/过期/长度/摘要；EditorSession正式新建只用manual，文件转写页目前只提供原始JSON制品下载，ASR→稿件导入桥接尚未接通。私有TranscriptTextExports可导出明确revision的精确UTF-8字节，但尚无正式校对页下载消费；字幕导出实现、消费者、测试明确留空，须先核对既有制品/合法时间轴语义，不能凭字数伪造时码。4个相关PG原CI节点和1个手工校对浏览器节点按原checkout提取并分别声明范围，本轮未重跑或加载模型。

C02/AC2–AC4新增23项映射，已覆盖C02全部29个稳定条件，但不等于C02通过。以下缺口与负例固定于原`70ab43d`快照；有效配置校验的后续修复另列，不能把旧证据改指新代码：

- TTS请求/日志固定原文本Asset、voice/profile revision及创建/取消原键；模型请求使用逻辑名，冻结协议由服务端一次解析并返回不可变effective_configuration.models.tts。C当前没有消费/核对该模型修订，也未对照原请求核对有效voice/profile，不能自行增加未冻结model_revision入参来绕过。
- **消费者负例已实际复现**：用Vite SSR加载当前类、只走内存dev transport并禁止global fetch；请求固定voice/profile，合成返回Job缺这两项有效引用时仍被确认，unknown=false。按failed/unit/synthetic登记，仅证明消费者核验失败，不声称真实引擎实际用了错声音。现有dev transport也未填入请求的有效voice/profile；请求正文相同的既有测试不覆盖此失败。
- 六个相关原CI Chromium节点按原checkout分别提取。实际WAV探针完成1秒/48044字节合成WAV解码、试听、下载字节对比及迟到/URL释放；其他探针分别是dev内存、production UI回环、built class非UI或OfflineAudioContext，不含真实Speech后端/模型/设备资格。原键、刷新、取消/Operation及410局部恢复有证据，通用SSE中断后的完整恢复断言仍缺失。
- 整包JSON/WAV下载不证明Range；当前客户端未发Range或校验Content-Range/416，也未计算内容sha256与JobArtifact交叉核对。WAV限定消费及离线Float32媒体探针不结算MP3/raw PCM边界；相应实现/消费者/测试空项明确保留。Asset/Artifact过期当前主要是合成错误/状态和原hint恢复验证，仍须真实生命周期/对象区资格。
- 同源公开客户端与窄恢复日志是凭据边界的源码证据，尚缺固定生产bundle/请求/响应/Storage的引擎、数据库、组件凭据排除验证。副本保存UI当前用ref保存重试键、unknown后可改目标项目的恢复风险已记录，留给C07/T09完整副本意图切片核对，不用同目标fake重试用例消项。

下一单元核对C00/AC1剩余3项及C07/AC3–AC6剩余23项；T02范围核清后按T03实施已确认的C自有缺口。所有原AC、M1/M2/M3和阶段放行范围保持不变。

C03全20个条件已复核，其中14项为新增映射、6项为历史映射细化。正式创建页分开设计/克隆输入，克隆要求材料用途确认，改素材编号/参考文本会取消确认；原CI精确声音Chromium节点确有相应断言，但使用合成回环HTTP，无模型或复用资格。候选、再生成、试听、复用校验、发布、退役/禁用/删除仍缺正式消费者；创建确认和材料确认均不替代发布确认，没有发布按钮也不能证明preview_only发布拦截。TTS手填固定voice引用不等于已发布声音的真实复用。

本次实际离线负例使用PublicVoiceCatalogue、内存MockTransport与冻结公开schema，禁止socket连接：契约合法published/disabled VoiceRevision均被拒绝，旧形状中与请求不同的voice_id/revision却被接受。适配器读取根级languages/audio_formats，而公开契约将其放在capabilities；工作台disabled值还拒绝公开契约要求的retired_at。现有Python91项与TS/DOM53项回归通过，但旧目录夹具未覆盖这些语义，负例登记为failed，不升级资格。下一步按T03修复C适配/身份与生命周期投影，并由T07核对A06/B05/A04具名生产制品；不等待整包，也不自行下载/加载模型。详细负例与摘要保存在本轮忽略目录c03-catalogue-wire-gaps.json及mapping_reviews。

**C03目录投影后续交付（2026-10-26）**：[PR #180](https://github.com/hrygo/SpeechNexus/pull/180) 的最终head `99a59be` 已合入main `5164b6a`（04:08:53），不改上文固定70快照的失败事实。新增公开源只读生成校验，映射capabilities并核对voice_id/revision；disabled保留退役时间及完整原因。冻结v1的128字符投影，新增v2支持2000字符并接正式Web，须先部署v2服务再部署新Web；无静默截断或回退。总期限、传输错误、重复键、256KiB正文及取消关闭均有反例。

最终CI40228628754全14项成功：Python6604项/223子测试、85.32%；六分片1710收集、1688通过、22个既有PG专用参数在InnoDB不适用，原生HTTP/实际C lifespan/最小角色PG/生产浏览器新增10项及原章节探针全部通过，无相关跳过；6分片PG和覆盖率PG均停止。807旧head漏同步JS探针v2路径导致整体失败，证据与10项已通过事实均保留；99仅修正精确路径，未删断言、跳过或放宽预算。实际合并树与CI测试合并树一致。proof位于`pr180-voice-catalogue/merge-and-ci.json`，源文件和原始报告有摘要。

这只完成公开目录读取投影，回环上游仍为合成事实，C侧PG并不证明底座声音发布持久化。#38已回读保持open/status:in-progress、原4条AC与未完成任务不变，并修正旧“消费者缺口归零”表述。T07最新owner正文仍确认A06/B05候选/复用/发布及A05/B04真实流未交付；#142继续公开持久链和真实取消预检。已归档具名依赖，不等待整包。下一独立单元修复C04已复现的播放关闭确认及ASR发送水位；完整Goal和全部278条件仍未验收。

C04/AC1–AC2新增16项映射，区分公开流语义、本地缓冲、执行硬上界及设备资格：ASR按样本credit并扣除后发在途量，TTS按UTF-8字节credit且冻结已ACK前缀；epoch/seq/本地generation、final正文和连续PCM有合成反例。采集待ACK帧和主线程pending有界，播放容量120000samples、单块8192及最多64条在途消息，TTS接收队列另限330000bytes/512messages。以上不证明引擎累计hard_output_ceiling_samples或目标长时内存，缺失的硬上界角色不拿ring-buffer断言填充。PCM只经私有临时音频通道，当前Host快照和恢复journal不含PCM；完整运行时缓存/长时堆资格仍待验证。

3个原CI节点已按实际checkout提取，未重跑：ASR/TTS是生产bundle加合成回环REST/WS；TTS每次48000samples被Worklet消费、两次累计96000，不证明真实模型/扬声器/长期播放。capture是真实Chromium图但fake device且绕过autoplay，含保留引用后的真实子frame导航释放和Permissions-Policy拒绝；不证明真人权限弹窗、物理设备切换或休眠恢复。当前11文件126项unit/DOM回归通过，范围仍为合成边界。

**播放清理负例已复现**：实际openTtsPlayback/createWorkletPlayback/PlaybackChannel通过Vite SSR加载，用合成AudioContext/AudioWorkletNode注入close拒绝并禁止fetch；Context保持running，snapshot仍为stopped，关闭失败被吞掉。此failed证据只证明清理确认逻辑缺口，不声称真实设备已经泄漏。采集端已有await close与capture_cleanup_failed传播，播放端须补延迟/失败状态和完整消费者反例。真实A05/B04流、水位、硬上界及目标设备资格具名依赖继续保留，C可独立修复播放释放，不等待上游整包。

C04/AC3–AC5新增20项后，全36个条件都有状态与边界，仍未验收。权限证据区分策略拒绝、host事件注入、fake设备及真人/OS操作；当前正式RealtimeController不传设备选择，页面无切换消费者；已读入口只调用getUserMedia，没有系统音频消费。支持矩阵须按原目标核实，不能将缺失格子静默改为不适用。隐藏DOM故障注入及实际iframe pagehide不替代真实系统休眠/后台节流；原REST键恢复不替代音频无缝重连。

固定同源REST/WS URL校验及内层nginx Upgrade配置已核对，但现有回环HTTP/WS证据不是批准远端HTTPS/WSS证书、代理和真实流资格。原CI真实C HTTP/PG/Chromium consent节点精确提取passed，覆盖零麦克风请求、初始ASR表单Tab顺序/标题焦点/360px及不可用服务拒绝；不含采集或真实Realtime。通用pages.a11y扫描应用初始页，不能结算实时运行/排空/错误/恢复的全部键盘/焦点/小窗口，更不替代实际读屏。长时行为亦不能由两轮合成96000样本结算。

历史实时Minor继续明确保留：**本轮实际复现ASR未发送水位**，生产Controller/AsrStream经Vite SSR配内存socket/capture（禁止fetch），容量拒绝后socket.send=0，中止发布sent=2400而accepted=0；现有overfull单元只检查未发送，不保护sent准确。已将C04/AC1/S04改为C待修复并附failed证据。另直接核对TTS终态禁用本轮停止按钮，done先到/缓冲未空时只能整会话abort，尚缺单独停止/会话复用行为回归；不因仍标Minor就忽略。当前另11项Host/URL unit/DOM回归通过，不覆盖上述缺陷。其他包旧Minor仍按原记录待逐项核对，未凭汇总数关闭。


C04后续修复已由[PR #182](https://github.com/hrygo/SpeechNexus/pull/182)于2026-10-26 04:41:29合入main `e959c43`；head `0320c51`基于#168的`ff224d9`，两补丁与原候选逐字一致。发送拒绝/抛错不推进ASR水位、credit、buffer/phase或命令身份；TTS由唯一关闭Promise记录closing→stopped/failed，done/刷新不覆盖本地清理，终态仍能停止缓冲。原ASR三项、TTS六项反例先失败；发送回退变异六项和UI状态放宽变异两项失败。精确CI40231216290全14项成功：Python6652/223subtests/85.48%，六分片1714收集/1692通过/22既有PG-only参数跳过，4项实时合成探针全部实际通过；Workbench691/Admin151/契约1870、52项自动可访问性及静态/构建通过，七个隔离PG均确认停止。测试合并commit `c5c8fc8`与实际合并树`b8748c9`一致；这是合前CI，不声称合后重跑。实际Worklet/native关闭延迟/拒绝不证明物理设备或真实服务。#39正文已回读保持open/status:in-progress、原5条AC及未完成任务不变；候选、原失败和70快照保留，78份原始文件按摘要冻结，5个子条件挂新增版本followup，全部278条件仍not_run。下一C单元先复现ASR控制器发起stop后未等待closed就显示idle的源码缺口，再修复本地所有权与诚实状态。

C04采集释放修复由[PR #183](https://github.com/hrygo/SpeechNexus/pull/183)于2026-10-26 05:09:14合入main `f245c00`，head `d8f91fb`基于A00 #181后的`046ae7f`。输入停止与资源确认分开：stopping立即停track/节点，closed等待native Context关闭；初始化中止保留清理失败，迟到设备由原owner释放，控制器不提前清空所有权。原生中断立即请求远端中止，本地释放独立等待；已读取远端释放后不因本地失败重复取消。底层/控制器/页面反例先失败，提前终态变异4项失败；构建失败、旧dist探针2失败及新探针回执时序4失败均保留，未放宽断言或期限。精确CI40233520591全14项通过：Python3.14.9、6706/223subtests/85.48%，六分片1718收集/1696通过/22与#182相同既有PG-only跳过，实时模块8项全部实际通过；Workbench704/Admin151/契约1870、自动可访问性52及静态/构建通过。7个隔离PG均停止；测试合并`8cf7a96`与实际合并树`fb76b5f`一致，不冒充合后重跑。#39正文已回读保持open/status:in-progress、原5条AC和混合未完成任务不变。104份原始文件按摘要冻结；5个子条件新增版本followup，原#182/70/失败事实保留，全部278条件仍not_run。下一独立C单元接设备选择/切换；现有底层支持deviceId，正式控制器/页面尚无入口。按蓝图从桌面Chromium/实际OS组合逐项取得支持矩阵，保留正文、显式gap/新身份及旧资源确认，不以本次fake麦克风代替真实设备/权限、实际读屏、长时或A05/B04公开实时资格。

C04设备选择由[PR #184](https://github.com/hrygo/SpeechNexus/pull/184)于2026-10-26 05:43:09合入main `15d91f8`，head `aa96f36`基于`f245c00`。设备列表只经手动枚举，选择须确认应用；旧输入/连接结束并标gap，正文保留，本地/远端释放确认后新建会话并显式开始，新采集使用原生exact约束。设备标识不进恢复日志/REST，不自动回退；原生名称搜索/应用/开始与标题焦点、360px经过正式bundle+fake设备实际验证。还修复GET已释放、接受时快照仍draining时pagehide/reload重复取消。控制器四项、确认应用及重复取消反例先失败，选择守卫变异四项失败；旧bundle/标签定位/End与方向键假设/重复取消失败全部保留，最小原生HTML对照后使用实际键盘名称搜索，未放宽断言或期限。精确CI40235979966全14项通过：Python3.14.9、6706/223subtests/85.48%，六分片1719收集/1697通过/22与#183相同既有PG-only跳过，本模块9项全实际通过；Workbench713/Admin151/契约1870、自动a11y52与静态/构建通过，7个隔离PG停止。实际合并树`8d3080c`与测试合并`4a54f1e`相同，不声称合后重跑。#39已回读保持open/status:in-progress和原5条AC未勾选；8个条件挂局部版本followup，218份原始文件按摘要冻结。

随后对同一正式bundle的只读焦点观察保留了新的负面事实：三次创建会话后焦点均落BODY，开始采集后停留禁用按钮。观察器仅增日志、原探针断言不变且通过，探针源码按原字节恢复；这些不是完整键盘/读屏通过证据。三次BODY结果登记为`c04-create-focus-observed-failed`，应用设备后的标题焦点通过不覆盖该失败。下一C单元从此反例修复创建/未知/拒绝/重放及采集状态的稳定焦点，再扩展全状态键盘/辅助技术；物理设备/OS权限/支持矩阵、长时、真实双工及目标HTTPS/WSS/GPU/拓扑仍须独立取证。原70和此前#182/#183失败/候选事实保留，全部41AC/278条件仍未验收，完整Goal保持active。

C04焦点候选[PR #186](https://github.com/hrygo/SpeechNexus/pull/186)已提交（head `5ce84db`、base `15d91f8`，精确CI40238179522运行中）。两Host按局部焦点归属修复disabled/移除后的回落；异步返回不抢用户已移动的焦点，外部点击、窗口失焦、隐藏/非活动有保护。新增复现23项中5失败→修复23通过；disabled变异5失败、字节恢复23通过。旧bundle的ASR/TTS创建均实际失败，新包最终9项/93个焦点检查点通过；保留原业务断言并使用enabled/focused前置的Enter操作。首轮build测试参数失败、lint及误启动旧包中止、新包键盘helper未等待enabled导致8通过/1失败均保留，修复后静态/构建、725/151/1870测试、52自动a11y通过。独立只读审查无阻断；候选原始日志138份与8个源码摘要冻结，3个条件挂版本followup，旧failed登记不覆盖。仍待CI/合并，完整读屏/设备/真实服务/长时/拓扑未验，全部278条件仍not_run。

T07同期核验：#168原生事实为head `27baf28`于04:30:12合入main `ff224d9`，默认文件ASR工厂不再是未交付依赖；其旧真实1.7B/CPU VAD实验仅证明fixture准入和私有staging，A04权威音频Asset/完整公开Job/Operation/Artifact发布下载与可信结算仍缺。已回读#23/#24/#31/#142并归档，A05/B04仍未交付真实双工，#142正文旧候选状态不覆盖已核实合并。下一步在新组合验证后继续C自有媒体/操作缺口，按具名制品接T08，不等待整包关闭。

C05全部23个条件现有状态与边界映射。会议支持保存稿件创建、固定版本封存与可选纪要；应用明确拒绝speech_session_id和save_audio=true，尚未保留实时session/gap或联合preview/final/辅助revision。封存追加版本、原键恢复与历史保护已有局部PG/HTTP/浏览器证据；独立用户要点、正式固定版本会议导出仍缺消费者，不能由LLM纪要或私有UTF-8导出helper替代。

提词本地固定稿与有界保守匹配算法已有单元保护，正式页面仍为手动粘贴/键盘/滑条阅读；本地稿修订和服务端project/script_revision位置元数据尚未贯通正式稿件版本，更未接真实语音跟随。旧context隔离不替代真实session/epoch映射及跟随中接管。普通会议和提词独立于LLM，但现有浏览器禁用LLM发生在生成纪要之后，不能结算“完全禁用启动→新建→封存”的完整断言。分人/对齐仍需具名正文版本制品与正式联合消费者。

本次C05复用了10个精确原CI节点，分为封存历史PG、会议HTTP、会议浏览器、提词位置状态及手动阅读五组，均保留实际checkout 2c53879且未重跑。当前新增65项Python及64项TS/DOM单元回归通过；原CI合成文字、fake LLM和手动页面均不构成真实语音/物理输入/模型资格。C自有要点、固定导出和版本接线可独立推进，不等待A05/B04/B06整包。

C06/AC1–AC2新增13项：无LLM配置时默认关闭，部分配置启动拒绝；助手创建在turn store前检查enabled，原CI真实HTTP/PG断言503且turn/receipt均0。固定数值loopback或私网HTTPS端点、服务端配置、trust_env=false、禁止重定向与单次请求保护已核对，但配置qualification_reference及内网地址不证明目标LLM/代理没有公网转发，仍须T11联验完整出站边界。

纪要短事务固定sealed稿件身份/版本/摘要、完整profile（含模板正文/修订/预算）与绑定摘要，claim/retry对原配置核验；running未知不自动重派，COMMIT结果未知按原任务/键查询，异常不另写模型失败。正式页面先持久完整原意图再请求。现有模型名字符串和配置绑定摘要未证明部署背后不可变权重/引擎版本，C06/AC2/S03保持C待补，先核对批准服务的制品映射，不自行添加未冻结字段。

本单元从原CI精确提取12个节点，分为默认关闭HTTP/PG、固定纪要意图PG和一轮dispatcher三组，保持实际checkout且未重跑。一轮dispatcher节点用fake LLM并由测试直接调用，不是完整ASGI lifespan或真实模型资格。当前174项Python及14项TS/DOM回归通过，LLM网络测试限合成transport及隔离loopback HTTP/TLS。独立的真实模型任务、服务lifespan和正式浏览器三项qualification仍未运行；原稿不变、绑定保护与局部测试不结算模型版本或整体AC。

C06/AC3–AC5新增22项后，C06全部35个条件都有状态与边界，仍未验收。当前正式WorkbenchAssistant只委托持久存储创建queued、读取和取消；runtime仅装配minutes/export/deletion循环，尚无助手LLM→公开TTS Job→结果执行链。配置LLM和speech client即报告available不能证明可完成；声音只固定id/revision，未核对执行侧已发布资格和有效声音。正式Host无播放器、插话、文本/TTS/播放generation关联，也无独立保存/导出动作；HTTP取消只修改本地行，不代表底座停止或资源释放，更不代表已计入同设备父预算。

**两个助手负例已实际复现**：领域start产生job非空、output为空，冻结schema要求job非空即output非空；running、启动后failed、启动后cancelled三个公开投影均被生成校验器拒绝，queued、无Job的失败和succeeded则合法。另一负例用真实WorkbenchClient/AssistantSession与内存transport/journal：首次丢回执后同键同体重放收到合法succeeded，客户端却仅接受queued，仍保留pending/unknown。现有Host重放mock只返回queued，未覆盖进展态回执。这些结果按failed/unit/synthetic登记，不是实际HTTP/PG/模型故障，也不通过放宽契约解决。

本次精确提取6个原CI节点：4个PG存储测试只证明queued/read/cancel/回执，失败行由owner SQL注入；2个生产浏览器测试只验证disabled/unavailable、零turn请求与该状态下零越界，均保留实际checkout 2c53879且未重跑。当前13项助手应用/领域/公开投影单元测试通过，但未覆盖上述负例。启用态真实HTTP/PG成功、取消、失败和重放，以及真实质量、播放/设备、保存/导出、内容网络和父预算仍缺证据；局部同源客户端、内网LLM适配、内层nginx与窄公开错误不能替代完整目标网络资格。C可先补状态/回执消费/诚实能力及正式消费者，按T07逐操作核验A04/A06/B02，按T11与A01/A07/B07联验实际预算/停止释放/部署，保持未交付范围。

C08新增42项并复核原有日志1项，全部43个条件均有状态与边界，仍未验收。通用pages.a11y访问两个入口的初始页，keyboardAndFocus只验证skip→main和焦点环；书籍表单Tab/Enter/成功后焦点回落/360px、删除与实时consent探针分别有局部证据。它们不覆盖所有动态业务状态，也不等于物理采集/播放、OS权限、真实断网重连、长时或VoiceOver/NVDA支持矩阵；C04/C06已发现的正式入口/播放/状态缺口继续保留。

删除预检固定版本/摘要/完整依赖，确认原子保存删除意图、tombstone与outbox；行与对象完成标记分开，缺对象适配/失败不结算公开成功，lifespan在yield前重放，运行sweeper持续处理欠账。无cleanup身份时真实HTTP返回503 deletions_unavailable且pending为空；危险身份拒绝启动。16个精确原CI PG/HTTP/MinIO节点与3个删除/键盘生产浏览器节点按实际checkout 2c53879提取，未重跑。真实浏览器的同库重启重放、其他项目副本保留，不替代独立备份水位/A07对象依赖图恢复或生产DSN/角色/受控迁移/镜像/目标拓扑。Ubuntu CI实际覆盖已有后端/PG/HTTP，不能称目标生产部署已验收。

**日志内容负例与局部修复**：基线70ab43d的三个入口——minutes队列读取、minutes单任务执行、deletion sweeper——通过_log.exception带出合成异常正文与traceback，failed/unit/synthetic证据继续保留。基于最新main f239e2d的[PR #166](https://github.com/hrygo/SpeechNexus/pull/166)（head754e985）改为固定错误日志，保留任务标识/下一批调度/重试/取消；新增3个测试先失败，修复后相关25项通过，并检查LogRecord不携带延后格式化的异常或栈。Ruff format/check、mypy和7条依赖边界通过；完整本机单元测试3269通过、5项controller warmup失败，在未修改主线runtime上复现同样五项失败，其中四项127.0.0.2绑定失败、另一项未达到就绪。保留本机限制；[CI40204047880](https://github.com/hrygo/SpeechNexus/actions/runs/40204047880)全部14项成功，22:57:54合入main `eef6c54`，合前测试提交b4dcf49与实际合并tree均为68e70aa7；不改controller、跳过或放宽断言。C08 issue #43仅同步进展摘要并回读，保持open/status:in-progress及全部AC未勾选；未改Gate。证据保留后临时修复worktree已官方归档，本地已合并分支删除、远端自动删除，原验收工作区及未提交改动保留。全局应用/代理/观测及目标部署资格继续按T11核对，无真实业务内容参与。

AppShell无条件显示“本地离线·音频不出网”，实际应用/代理/浏览器/观测资格须由T11验证；配置内网地址、同源客户端或关闭态零请求不能证明整链不外发。共享部署/无登录文案已有正式消费者，但完整用户与运维/恢复交接、支持矩阵、旧Minor和浏览器抖动仍需逐项收口。此前固定读取main `639b703`的中英文README及docs索引：PR164/165已更新根README，docs/README仍声称尚未实现语音或工作台业务，已作为版本明确的文档不一致登记，未把旧工作树README当作当前主干。

C00/AC1最后3项确认：Admin/文件SSE及ASR/TTS实时事件消费同源生成parse入口，本次10项Admin、34项工作台事件单元通过；尚未交付的streamExportEvents阻止“完整计划范围”结算。三个开发演练入口必须DEV且显式dev-scenario，内存transport不调用native fetch，页面能力标contract并说明合成来源；合成Asset.status=ready不等于能力ready。正式构建中dev查询不可启用及演练无真实遥测的行为反例仍需补齐，不从源码单独宣称完成。

C07/AC3–AC6最后23项核对了实际端口、存储、路由和正式面板。通用PublicArtifactSource校验实际字节摘要/长度、artifact/workspace/receipt及过期，保存先对象后引用；C07 RenderSource仅固定标识与依赖摘要，未装配正式生成/来源/保存桥接。导出/副本公开二进制下载仍未注册，面板明确无下载链接。独立producer/进程、完整固定清单、检查点/续作、CPU/内存/磁盘/跨进程并发预算，以及质量/试听/匿名确认记录均有C自有缺口；不以批量limit、进程内串行、casting revision或progress计数替代。

**导出格式/缺章负例已实际复现**：实际ExportWorker配合明确标注的测试producer/store/object-zone，在请求ZIP但返回非空非法ZIP、零章节上报时仍写入fake对象并调用complete；独立ExportJob.complete随后把进度填满为2/2。这里只证明worker/domain的保护缺口，不声称真实PG提交或实际全书导出成功。本次49项现有导出/副本单元通过，未保护该反例。原CI的取消、PUT/回执故障及PG行锁测试按原checkout保留，不结算跨进程恢复/唯一发布、实际格式、真实模型或目标拓扑。C07全范围在C06关闭时的正式链路、关闭工作台不影响底座仍须运行取证；具体上游制品按T07逐操作接收，C实现可独立继续。

## 已提交内容下载候选

[PR #177](https://github.com/hrygo/SpeechNexus/pull/177) 当前候选 `3246cc0`，base main `250cf1c`，49个文件。两个公开操作接 C 短事务/私有对象区/正式运行根及正式 Web 消费者，发送前校验完整长度和摘要，支持单段 Range、416 和大小/并发/协作时限。源服务下线不妨碍独立副本下载。浏览器核对完整字节/摘要后保存；取消焦点丢失和重复选择当前项目清空书目均先复现、后修复。

本地 Python3.14.9 相关2816项、rebase新增字面量契约66项、HTTP组合208项分别通过；工作台675、管理端151、TS契约1838及基础a11y52通过；Ruff/mypy771/7依赖边界、32机器契约、漂移/结构检查通过。摘要检查mutation被ASGI坏内容用例检出。本机未启动数据库或模型；基础a11y并不代表实际辅助技术/设备资格。

旧head91的CI40220610290保留两种失败：Python启动MinIO时临时DNS失败，未进入测试；生产浏览器暴露重选当前项目清空书目，已修复而未移除实际点击。随后head68的[CI40221540971](https://github.com/hrygo/SpeechNexus/actions/runs/40221540971)六分片1696收集/1674通过/22既有PG-only不适用，原始报告本地完整/无重复/同checkout `ce2a092` 守卫通过；8项相关runtime与实际生产键盘浏览器均passed，六个一次性PG已确认stopped。Python实际6505通过/3失败/184subtests：仅3个路由数量断言未计新增两条路由。当前324只改这3份测试，精确数量改为18/23/20，并补操作ID/路径/416及未配置503断言；208项相关HTTP组合通过，产品字节与68相同，原失败未隐藏。

**已于2026-10-26 03:02:15合入main `85a4d824a96f2eed61af998e0470a78edc81b159`**。最终head的[CI40222511230](https://github.com/hrygo/SpeechNexus/actions/runs/40222511230)首轮Python6508通过、184子测试、覆盖率85%，覆盖用PG已停止；Web与静态检查通过。分片0在Playwright系统依赖下载时达到10分钟限额，分片4因依赖安装耗时后也超时；GitHub超时注释与原日志保留。仅重跑失败作业的attempt2最终14项全部成功，未更改测试、限额或保护。六分片raw报告1696收集/1674通过/22既有PG-only不适用，8项相关runtime/生产键盘浏览器均passed，6PG停止；原完整/无重复/同版本守卫通过，最长185.38秒。测试checkout `cf18c984f48d10491d9c24b9e545a910f513acfb`与实际合并tree同为`d32847b34f4ab64d8c4bc59fa9e8b3e92732b390`，仅为合前CI。A00已在PR评论确认固定共享制品及消费者先升级边界无新增阻断；不以该审查替代业务资格。#42部分更新已回读：正文2950字符（原2780，增长6.1%），进展475字符，全部原AC及依赖不变，仍open/status:in-progress。完整证据保存于 `.runtime/team-c/next-phase/2026-10-25T155546+0800/pr177-public-downloads/`；candidate-3246cc0.json固定49文件及前一轮精确结果，candidate-68及历史失败保留。C07/AC3/S03/S05/S06以独立followup_delivery关联已集成版本及merge-and-ci.json，保留70基线角色/失败和候选记录；所有278项继续not_run。完整清单/检查点/续作、正式saved-audio producer、SSE、全局预算、真实质量/试听/匿名确认及目标拓扑仍保留。

正式saved-copy页面浏览器下载及实际过期/删除失败路径已由后续#179补验，见下节；公开源生产者/非空receipt、全清单/检查点/预算及完整C07资格继续待验。

T07最新远端元数据：PR168 `21129b4`、PR176 `6b09ad9`及PR178 `d772d31`均open。#178仅以#168为base的3份文档；owner报告在旧5f72b20上完成默认控制器Docker/SQLite/TLS→默认网关record_stop→A原lease/base分步结算，证据前拒绝释放、unknown保留占用、重复结算幂等；预热139.745秒、stop6.090秒/exit0。该报告的准入/就绪provider/owner过期仍为fixture，原始产物在B本地，本轮未独立复验或转挂新head，不授予C公开Job/结果/真实闭环、单请求取消或物理集群资格。#168旧base250cf1c上的CI成功不覆盖#177后的组合；#176仍待标准runner实际prefetch/完整门禁，C未复制其调整。具体快照保留在pr179-download-acceptance/t07-dependencies-current.json；继续C自有工作，无跨chat消息、真实数据修改或模型加载。

## 下载补充验证候选

[PR #179](https://github.com/hrygo/SpeechNexus/pull/179)，head `6b2158d44715f2060a3e802e22efb347f79276c8`、base `85a4d82`，4个测试/探针文件。生产页面先显式读取独立副本元数据、再用键盘触发实际保存并核对落盘字节/摘要；保存后关闭合成源，检查不再请求来源。render result/job/generation由明确标注的合成fixture写入，公开副本创建/关联读取/对象区/页面为正式路径，不证明真实generateRenderUnit。

补充另一workspace、未知id及公开删除计划/确认后的副本404；导出queued409、真实runtime产生合成ZIP后经实际retention事务过期410、公开项目删除或另一workspace404。负例使用Range且不泄露ETag/Content-Range，并直接读取仍存在的MinIO对象，排除“只因对象被清理才拒绝”；测试延后定期清理，不能作为清理SLO。workspace是服务端配置边界，不引入产品身份/RBAC。

本地Python3.14.9相关59项单测、Ruff/mypy771/7依赖边界、JS语法/格式及Web lint/build通过；本地12项仅收集的记录保留。随后精确head的[CI40224858285](https://github.com/hrygo/SpeechNexus/actions/runs/40224858285)首轮14项全部成功：Python6511/184子测试/覆盖率85%，覆盖PG停止；六分片1700收集/1678通过/22既有PG-only不适用，12项相关runtime/下载/实际生产浏览器全passed，6PG停止，原完整/无重复/同版本守卫通过，最长264.40秒。2026-10-26 03:19:56合入main `2671d3296a940d98587c1766e9f7d0d2ced3c03e`；测试checkout `2ab19a5adb255582a95d953e1a2558a7c811564e`与实际合并tree同为`936c121adbd7bc83f86387a4ef29ad10ff264ebb`，仅为合前CI。#42部分更新已回读：正文3151字符（原2950，增长6.8%）、进展584字符，原AC/依赖与metadata均不变，仍open/status:in-progress。证据在`pr179-download-acceptance/merge-and-ci.json`，候选记录保留；3项C07子条件的verification_followup更新为integrated_partial，不改原源码角色和全部278项not_run。下一步补C03已复现的公开音色目录契约适配/身份和生命周期缺口，继续按版本接收真实公开生产者；完整C07、M1/M2/M3及Goal范围不变。

## 导出完整性候选

[PR #174](https://github.com/hrygo/SpeechNexus/pull/174) 基于 main `dbbffe2`（#172），稳定ID修正后的候选head `cc50758b6c41415496f66f32b6956ea37c0b0bf1`。原70快照的非法ZIP/零进度负例保留；新版本独立修复，不改挂旧证据。完成必须已有持久完整进度，回执匹配export/book/manifest revision/格式/总数；producer退出后的迟到report拒绝。实际WAV/MP3解码与ZIP中央目录/CRC/安全路径/成员数量/ZIP64及限额检查先于任何写入意图和PUT；校验结束复核取消，reserve_export继续锁内复核。新增反例先复现校验失败与持久取消交错卡在cancelling，再修复为只在明确状态冲突时结算取消，提交未知仍传播。

Python3.14.9 / PyAV19.0.2，当前head针对性158项通过；Ruff格式/检查、mypy763文件、7项依赖边界、32份契约检查及Web格式通过。首次backend单元/契约3936通过、7项因新工作树缺TS依赖失败；锁定安装后7项复跑通过。相关35项PG/MinIO/runtime测试已收集，新增持久零写入意图/零对象、缺成员/部分进度、取消后错误及实际组合根限额反例，本机未启动这些服务。独立复审确认P2消除；写入意图查询以sha256(export_id)匹配真实key_digest，成功正例也断言1，避免错误查询恒零。

首轮[CI40215652724](https://github.com/hrygo/SpeechNexus/actions/runs/40215652724)的六分片在requirements收集阶段失败，容器均正常；pytest自动把新的合成ZIP字节写入nodeid，超过既有4096字符上限。保留失败日志，本地同入口先失败后改成稳定短ID并通过，全部用例和原守卫不变。随后head `cc50758`的[CI40215905666](https://github.com/hrygo/SpeechNexus/actions/runs/40215905666)全部14项成功：Python6384/184子测试、85.28%，六分片1690收集/1668passed/22既有PG-only不适用，35项导出PG/MinIO/runtime全passed，6份报告确认PG停止；实际checkout `3da3fcf`基于`dbbffe2`+`cc50758`，本机严格原守卫再次核对完整分片。因主线#173合入后受保护状态BEHIND，已rebase到`78505a8`，当前head `9a96267f610bc400b9ae53e5202cdda274587894`，19个功能文件逐字节未变、158项再通过；最终[CI40216653243](https://github.com/hrygo/SpeechNexus/actions/runs/40216653243)全部14项成功：Python6384/184子测试、85.25%，六分片1695收集/1673passed/22既有不适用，35项导出场景passed、6份PG报告及全量覆盖PG均确认停止。2026-10-26 01:35:55自动合入main `39aeb41941a1cf248cb32d280ab502c9cc780152`；测试checkout `751d8e2`与实际合并tree均`c77ef7f7c098319d16092177b66a09fbceb906e2`。仅为合前CI。#42进展正文更新后回读一致：446字符/正文2780（原2608，增长6.6%），原全部AC与未完成任务保留，仍open/status:in-progress；5项条件关联版本化局部证据，278项均仍not_run。证据放入`pr174-export-integrity/`。

这仅是发布前完整性单元：同数量的错误单元、重新封装的截短音频不能只凭解码识别。完整不可变单元/版本/source receipt清单、检查点/崩溃续作、独立worker/正式saved-audio producer、公开二进制/SSE消费、父预算与真实质量/试听/匿名确认仍待实施。协作期限等待native调用返回，不等于硬CPU/内存/磁盘或并发预算；不改变原41AC、278条件或M1/M2/M3状态。

## ASR原稿导入与固定版本TXT

2026-10-26新增独立候选[PR #170](https://github.com/hrygo/SpeechNexus/pull/170)，原候选`b8e9d31`基于`a43aacd`；主线新交付#169后已rebase到`7dc276a4cfa38e8f433def05b0cbed25dcec8d5d`，head `338a87a69d18c6983ff8610bcd8e18f223ca8743`；其CI完成后主线文档#171合入，严格保护拒绝旧base合并，现rebase到`d87658525b701be3865fcf15d5016efa53913834`，当前head `5bc0b5a4d7a4491aa03a0db3e1b9af1cb43b7e15`，25个功能文件逐字节未变。正式ASR成功结果须匹配Job.artifacts的transcript与回执，用户显式选择项目并命名后才导入；已有手工输入/校对草稿不被覆盖，未知提交持久保留完整来源请求和原键，重载不自动写入。来源适配器原先只接受text/plain，与网关实际生成的既有Transcription JSON存在断点；现在复用生成类型/校验提取精确原文，保留原身份、ready、workspace、过期、长度与SHA-256检查，并拒绝重复字段、非法Unicode/NUL和过深JSON，不改wire或新建路由。

Python3.14.9来源46项通过；去掉生成校验的mutation被负例捕获，恢复源码后46项再通过。rebase后完整Python契约2700项、Ruff格式/检查、mypy753文件及7条依赖边界通过；完整Web格式/lint/类型/测试/构建通过（Workbench666、Admin151、TS契约1834）。生产包+合成HTTP的实际Chromium与pytest执行守卫通过，5上下文/10次写请求，覆盖ASR结果进入正式稿件页、提交后丢回执、刷新只读与原键原体重放、封存原文/独立校对、带BOM/Unicode/换行的固定版本TXT原生下载，排除未保存草稿，越界请求/页面错误为0。BOM误判及已有编辑器下导入入口隐藏均先复现后修复；历史失败与rebase前/后日志分别保留。52项初始页面可访问性曾在rebase前通过，不外推为全部新状态/读屏资格。

证据保存在忽略目录`pr170-asr-manuscript/`，25个候选文件摘要与提交逐字节一致。前一head的[CI40209800309](https://github.com/hrygo/SpeechNexus/actions/runs/40209800309)14项成功，Python6275项及184子测试通过、覆盖率86%，52项初始页面a11y通过；六分片1639收集、1617通过、22为原有PG-only/InnoDB不适用项。精确提取4项纯文本/JSON隔离PG导入/恢复/固定导出及实际浏览器守卫，均passed且PG停止，本机未运行这些PG场景。分片4首轮因Ubuntu镜像下载系统依赖约8分钟而触发10分钟总时限，报告缺失不计通过，保留日志后同head失败job重跑通过；本地合并报告检查最初指向嵌套目录失败，复制原始6份JSON到平铺目录后严格原守卫通过，没有放宽检查。最终head `5bc0b5a`的[CI40211340749](https://github.com/hrygo/SpeechNexus/actions/runs/40211340749)也在保留一次同类基础设施超时、仅重跑失败job后14项成功，未改工作流/测试/阈值；Python6275项及184子测试通过、覆盖率86%，Workbench666/Admin151及52项初始页面a11y通过。新checkout `59f5031349d7a18302205d042bea704b2763ce44`的全部6分片1639收集/1617passed/22既有不适用项，4项PG及实际浏览器守卫均passed，6份PG报告确认停止；2026-10-26 00:38:43受保护自动合并到main `9d277646affe7b4e9da8c216e088572be4dab60b`，测试与合并tree均`97b51d12bbfd899a737a5a762bf64d7a8de342be`。仅为合前CI，不称合后验证。#37进展正文写后回读一致，471字符/正文1540字符（原1471），任务/AC勾选及元数据不变，仍open/status:in-progress；原5项子条件只追加显式版本的局部证据，原70源码/旧失败事实保留，全部278项仍not_run。TXT不含时码，字幕导出、真实ASR公开生产/发布/预算、物理设备、目标拓扑及完整AC仍待验证。

T07还核实[PR #169](https://github.com/hrygo/SpeechNexus/pull/169)已以`7dc276a`合入main，提供saved-asset/book-export二进制和六成员export SSE生成SDK/校验/原生流，保留既有v1/v2 JSON选择。它没有注册C业务路由，不能据此认定生产下载/完整导出已交付。C可立即接对应消费者并验证实际字节/摘要/Range/事件生命周期；不等A00整包关闭。快照另存`t07-pr169-sdk-review.json`。另复核[PR #171](https://github.com/hrygo/SpeechNexus/pull/171)在冻结`7dc276a`上的协调证据文档：18端口映射不等于完整Scheduler/Worker、公开SpeechJob或真实停止/预算释放装配；文档明确保留这些缺项。仅作上游材料复核，无新增本机服务/数据库/模型运行，快照`t07-pr171-coordination-review.json`。

## TTS有效修订消费者的后续集成

[PR #167](https://github.com/hrygo/SpeechNexus/pull/167)基于`eef6c548947964b639f0dccb3855f696da89b77c`，head `509aabf08d2c1d3392e9e39521735593e5438afa`，于2026-10-25 23:28:11合入main `a43aacd1ad4b89579923b29607280aeadc5b17fe`。正式TTS逐次核对有效voice/profile的id/revision，要求有效TTS模型；模型请求允许alias，首次有效模型id/revision在展示前作为有界元信息持久保存，后续读取、原键恢复、事件及取消回读均拒绝漂移。界面显示实际模型/声音/参数修订。旧v2记录与原请求体保持兼容，仅能从完整有效Job补当前模型确认，不能补证旧记录此前的一致性。存储失败或错回执继续保留原身份及未知状态。

最初36项语义反例修前均失败，新增显示测试修前失败；新增41项测试，最终相关两文件64项通过。移除模型确认不可变保护的mutation实际被测试捕获，源码已恢复并与提交字节核对。完整Web格式/lint/类型/测试/构建通过；生产包加合成回环HTTP、原键恢复、dev/生产隔离三个Chromium探针通过，覆盖错误有效配置、重载模型修订漂移、合成WAV试听/下载和迟到结果隔离，越界请求均为0。[合前CI 40206470028](https://github.com/hrygo/SpeechNexus/actions/runs/40206470028)共14项成功；实际checkout测试merge `60f3d3152085e84eedde6b50cd180823448152fd`与实际merge树均为`18cd856f3f9da8d0685cadde9b7bc2a99a099421`，未称为合后CI。

证据保存于忽略目录`pr167-tts-effective-pins/`；三项固定修订子条件关联新候选/集成证据，原70源码与失败记录保留。#37仅更新进展，写后回读一致，任务/AC勾选与元数据不变，仍open / status:in-progress。真实TTS/模型、服务端不可变发布与停止、格式/设备/目标部署资格未验收，全部278项仍not_run。固定70快照的严格审计仍为126个映射角色缺项及2份历史附属摘要缺失，不将PR合并替代新的共同基线映射及完整AC。

## T07具名ASR制品复核

本次复核保存完整PR快照于`t07-asr-deliverables-review.json`，仅为制品/设计审查。[PR #163](https://github.com/hrygo/SpeechNexus/pull/163)已以`f239e2d62d1f016a540de11c369d1c899c17304d`合入，提供generation原生凭据与全目标核对；有效设计`controlled-asr-generation.md`仍明确默认真实factory、A端参数/发布与预算/公开SDK/C下载未接齐。#162当前head `6c53e03108adcdb0a2073f80c24ce42353cc5e03`的正文旧CI对应另一head，不能外推；#155远端head `6e7a39a44a8f143eb37acea4ec43ef2ef0fbe198`仍是A端受控运行制品，text-only验证不证明音频Asset ready。

T08真实ASR仍需A02/A04提供音频Asset ready、公开持久Job/Artifact/回执、发布、原身份恢复/下载的同版本隔离正式装配；B01/B02/B03提供固定ASR/VAD/引擎、受信实例/完整阶段图、默认文件执行工厂及真实执行/停止记录。按具体制品解除依赖，不等整包关闭；C继续补齐ASR→稿件→固定版本导出、操作映射与已发现消费者缺口。未启动真实模型、访问私有端点或跨chat发送消息。

## 17:02历史记录

以下记录对应当时未提交工作树，保留原始版本、失败、局部测试及限制。其后主干集成以本报告上面的当前基线及PR证据为准。

## 版本与集成边界

| 对象 | 固定身份或状态 |
| --- | --- |
| C 候选 / 当前 HEAD | `6d3d092990b9f8528831d7d154feecffebe6b82f`，PR [#153](https://github.com/hrygo/SpeechNexus/pull/153) |
| 共同集成 base | `33233c8a42b284b627e891b32b1b28faeafb30e7` |
| 执行分支 | `codex/team-c-next-phase`，托管隔离 worktree |
| 本地合并 | `git merge --no-commit --no-ff`；两处文本冲突已解决，未提交，`MERGE_HEAD` 保留；HEAD 仍是原候选 |
| 冲突解决后的 index tree | `853335ec2aa01ee69e8508b7df613a50e7f27737`；不包含之后的未暂存修复，不是最终待交付树 |
| GitHub 快照 | 15:55:46：PR open/unmerged、mergeable=false；该 head 的 status/check run/PR workflow 列表为空 |

主线传入的 419 个文件是既有上游成果。冲突仅涉及计划索引与消费者样例：索引保留双方条目；样例按身份严格三方合并，base 267、C 329、main 341，合并 403，无同身份相异修改或删除。未用整份旧文件覆盖另一方。原工作区既有 `AGENTS.md` 和其他任务文件保持原状。

## 实际修复与反例

1. **书籍页 Token**：[books.css](../../web/workbench/src/books/books.css) 使用未声明的 `--danger`，设计 Token 门禁拒绝。恢复提示改用共享主题已有的 `--warning`；lint 与受影响工作台构建通过，没有新增兼容别名。
2. **手工建稿探针**：[workbench-manuscripts-probe.mjs](../../scripts/dev-env/workbench-manuscripts-probe.mjs) 将 `getTranscript` 的 sealed 原稿样例用于手工创建；正式客户端要求 draft，正确拒收后保留 pending。修复只调整探针创建样例及对应流程，保留产品校验和已有原键恢复场景。修复前探针因核对按钮无法启用而失败；修复后 Chromium 实跑通过，4 个上下文、7 次写请求，draft/null、原键恢复、冲突及历史断言成立。
3. **实际调用守卫**：`tests/integration/probe_execution.py` 与 [promoted 探针测试](../../tests/integration/test_workbench_orphan_probes.py) 记录真实启动的脚本、命令和退出码；当前覆盖 15 个补入门禁的探针。15 个实际运行记录均对应正确脚本且退出码为 0，pytest 场景断言全部通过。单独的 mutation 保留脚本引用及伪造结果、删除实际调用，测试体虽然通过，teardown 仍以 `must launch exactly one subprocess, got 0` 报错，整体退出 1，符合反例预期。


4. **导出失败结算**：[export_worker.py](../../backend/src/speechnexus_backend/workbench/application/export_worker.py) 原先在对象写入之后才构造并校验 `ExportArtifact`，且发布异常不在单任务失败处理范围。空字节/非法元信息会外抛，写入失败还会中断整批已 claim 的任务。4 个单元失败用例复现后，将域元信息校验移到对象写入前，并把发布阶段纳入该任务的稳定错误结算；数据库完成操作仍保留在异常补偿之外，防止提交回执未知时把成功改写成失败。
5. **导出诊断内容边界**：[runtime.py](../../backend/src/speechnexus_backend/workbench/runtime.py) 的 export loop 原来用 exception 日志输出异常正文及 traceback。合成内容回归确实失败后，改为固定诊断消息；循环继续下一 pass，取消仍传播。只覆盖这一日志入口，不能推断所有观测均合格。
6. **预设草稿消费者**：[ProfileHost](../../web/admin/src/profiles/ProfileHost.tsx) 已挂入正式 Admin，消费已有草稿创建/固定修订读取协议。完整配置需显式填写、核对、确认；发送前记录原请求与原键，未知结果只能原键恢复，固定读取不能结算写入。保留精确十进制 CAS、拒绝静默重基、旧响应隔离与存储失败关闭写入。没有接入发布、激活、退役或宣称运行资格。Admin 19 个测试文件共 145 项通过（含新增 27 项）；全仓 Web 类型检查、lint、格式检查和 Admin 构建通过。构建仍有大于 500 kB 的 chunk 提示，未为此扩大修改。该切片只有合成客户端/会话/DOM 证据，尚无实际浏览器、真实服务持久化或目标部署资格，不勾选 C01 AC。

运行记录只证明子进程成功结束，必须与场景断言共同使用；守卫不替代业务断言，也尚未覆盖其余必跑探针。上述浏览器场景使用合成 HTTP/媒体，不属于真实引擎或物理设备资格。

## 本地验证结果

命令从隔离 worktree 根执行；完整日志保存在忽略目录 `.runtime/team-c/next-phase/2026-10-25T155546+0800/`。锁定依赖安装通过，未升级依赖。证据对应当次未提交工作树，不能写成最终 head 的 CI。

| 检查 | 实际结果与适用边界 |
| --- | --- |
| `make contracts-drift` / `make contracts-check` | 通过；生成无漂移，32 个契约文件通过校验 |
| `uv run pytest tests/unit tests/contract -q` | 最新 5546 passed，137.22s；包含运行守卫与导出/日志修复后的完整单元/契约回归 |
| `pnpm test` | 通过；Workbench 597、Admin 118，契约包测试通过 |
| Python format / Ruff / mypy / lint-imports | 通过；后续新增文件再次检查，mypy 最终覆盖 713 个源文件，依赖边界 7 kept / 0 broken |
| Web format / lint / typecheck / build | 通过；Token 修复后重跑 lint 与工作台 build；构建有大于 500kB 的 chunk 提示，未当作失败或借机重构 |
| `uv run pytest tests/integration/test_workbench_orphan_probes.py -q` | 15 passed，51.76s；15 份实际执行记录逐一核对 |
| 守卫单元与 inventory 回归 | `tests/unit/test_probe_execution_evidence.py` 与 `tests/integration/test_probe_inventory.py`，10 passed |
| 移除调用、保留引用的 mutation | 预期失败，退出 1，明确因缺少实际子进程报错；不纳入通过计数 |
| `make contracts-compat` | 针对固定 base 通过，只覆盖正式声明的 OpenAI 子集 |
| `make contracts-speech-changes contracts-coordination-requests contracts-coordination-results contracts-sdk-test` | 通过；SDK 3 passed；各结构检查的未验证范围继续有效 |
| `make contracts-http-changes` | **失败**，6 项结构发现，详见下一节 |
| 导出隔离 PG/MinIO 集成 | 19 passed，7.55s；三个文件覆盖 worker、导出存储及生产运行时派发。PG 18.6 仅 Unix socket，独占 MinIO 容器；producer 为 synthetic adapter，写失败及丢回执为明确故障注入 |

隔离集成实测：空字节与非法媒体元信息持久化为 failed，artifact 为空且对象前缀无写入；首个对象写入失败后，同批第二任务仍成功且字节一致；数据库实际完成提交后注入丢回执，查询仍为 succeeded，下一轮不重复 claim。PG 进程已停止，独占 MinIO 容器已停止并自动移除，集群测试数据保留在忽略目录。完整身份及清理结果保存在 `export-pg-minio.json`，未接触共享数据库/对象区，也未加载模型。

这些结果只证明 Mac/PG 18.6 上所列应用及持久化路径；producer 不产生真实语音，故障注入不等于真实网络丢包。未运行双库协调、真实 ASR/TTS、VoiceOver、物理采集播放、Linux/GPU、完整内容网络、长时或集群故障验收。非空但无章/不可解码的容器、固定清单/检查点、进程崩溃后的 running claim、预算及真实 `ExportProducer` 仍未通过，不能将空字节检查升级为 C07 全范围资格。

## HTTP 兼容性待解决项

针对上述 base，工作台契约 `0.1.0.dev0 → 0.1.0.dev0` 有 6 项发现：

| 变化 | 当前判断及下一制品 |
| --- | --- |
| `RenderUnitCreateRequest` 新增必填 `model_binding` | 原请求只必填 ordinal/text/voice/model，旧消费者不能直接沿用。C 作为该工作台协议 owner 须明确版本与迁移方案，列出旧消费者升级及历史资源处理；不能用当前可变默认值补造原执行绑定 |
| `getWorkbenchStatus` 响应新增受约束的 `minutes_profile` | 原基线无此显式属性，比较器报告 boolean schema acceptance/guarantee changed。须审查新增约束与旧消费者的接受范围，提供兼容或版本化切换证据，不能把发现自动解释为运行时故障 |
| 创建、读取、修改渲染单元，以及生成响应的 `model_binding`，共 4 处 | 原基线无此显式属性，当前引用固定资源类型，比较器不能证明相容。与创建请求一起提交生产者/消费者升级清单、正负样例及固定绑定的恢复用例 |

建议先形成明确的新版本/同版本消费者切换方案，保留旧基线比较结果，并核对部署与历史数据影响；这是待评审建议，尚未修改公共协议或执行迁移。[固定主线中的现有规则](https://github.com/hrygo/SpeechNexus/blob/33233c8a42b284b627e891b32b1b28faeafb30e7/docs/architecture/http-contract-change-checks.md)明确版本升级不豁免发现，不能改 base、删检查或放宽 schema 来取得通过。原始失败保留在 `contract-change-gates.log`；后续检查另记，不能覆盖该失败。

## AC 映射与后续

[原 41 条 AC 语义矩阵](2026-10-25-team-c-original-ac-matrix.md)保留每条原文、issue 正文更新时间与 SHA-256，并给出 278 个稳定标识的语义子条件草案；原来的 115 个无损子句仍保留。38 个子条件关联已直接检查的源码或明确未交付的设计边界、正式消费者、候选测试及摘要；其中草稿切片的 10 项更新为当前实现、有限证据和具名依赖。所有 AC 和子条件均未判通过。文件/符号锚点及日志摘要已核对，但完整映射、人工语义覆盖复核和长期门禁仍待完成，T02/AC-02 未通过。

| 已确认的 C 自有缺口 | 当前实现与边界 | 下一制品 |
| --- | --- | --- |
| C01 管理写入与真实预算 | 正式 [Admin 装配](../../web/admin/src/App.tsx)已挂预设草稿消费者，具备合成测试证据，实际浏览器/真实服务待验证；[WorkspaceClient](../../web/admin/src/workspaces/workspace-client.ts)只允许 GET，诊断明确预算/租约/计量未接入 | 补草稿联调；逐项消费 Binding 等已交付制品，预算来源另行核对；发布/激活/退役须按具名生产协议接入 |
| C03 候选/试听/校验/发布 | [VoiceCreateHost](../../web/workbench/src/voices/VoiceCreateHost.tsx)明确只接创作任务；工作台声音应用服务是只读已发布目录 | 接收 A06/B05 的具名候选/试听/复用/发布协议并实现 C 正式消费者，真实设计与克隆资格分别取得 |
| C07 角色/生成/历史与完整导出 | [BookRenderPlan](../../backend/src/speechnexus_backend/workbench/domain/book_render_plan.py)有领域局部失效测试；生产存储固定 narrator，公开 render-unit 路由只有列表/创建/读取/修改，不含生成；当前运行时仅接收可选 producer 注入 | 完成正式角色/生成/历史消费者；补真实 producer、固定清单/检查点、预算及恢复，保留本次空字节/单次写失败修复的有限证明范围 |

下一步继续 T02 剩余子条件映射并交付缺映射/失效证据/资格误用门禁，依据上表逐个补 C 自有消费者；同时把 6 项兼容性发现形成具体版本与消费者升级制品，把实际调用守卫扩展到全部必跑探针。A/B 依赖按 T07 具名接收；原 41 条 AC、M1/M2/M3 与 AC-01–AC-12 保持完整，未经实测不升级状态。没有 commit/push/PR 合并或发布，C 不关闭 G0–G5。
