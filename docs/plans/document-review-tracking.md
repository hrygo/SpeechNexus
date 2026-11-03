# 文档逐篇审阅清单

本清单用于跟踪文档用途判定，以及对仍作为当前设计、操作或验证依据的内容所做的核对。既往修改或链接检查本身不代表整篇已审完。历史快照保留原时点事实，无需逐条与现状对齐；仅在其仍承担当前指导、存在误导性入口或需要纠正已知事实错误时处理相关部分。不能仅凭目录名或日期断定文档没有持续用途。

完成一篇后记录用途判定、核对依据和结果；部分核对注明剩余范围。确认仅为历史记录且无需调整的条目标为“历史保留”，不表示其原始证据已逐项复核。尚未判定用途的条目继续标为待核对；新增或迁移文件时同步调整清单。

2026-11-04 对 `docs/` 下 Markdown 的普通内联相对链接做文件目标存在性扫描，未发现缺失；该扫描不覆盖锚点、图片或引用式链接，也不验证链接内容是否适用。

2026-11-04 将清单路径与 Git 跟踪的 `docs/` 制品比较：双方各 331 项，无遗漏或多余路径；该清点不证明各项正文已完成审阅。

2026-11-04 核对交付矩阵及其结构化来源中 67 个去重后的仓库固定提交文件引用，本地 Git 对象均可解析；未逐项验证引用内容与结论的适配性，也未验证外部链接或远端可访问性。

| 文档（相对仓库根目录） | 状态 | 核对依据与结果 |
| --- | --- | --- |
| `docs/README.md` | 部分核对 | 全文阅读，核对本地链接、CI 数据库版本、上线前检查、Job 派发、同步转写、运行投递、网关停止恢复、资产存储、完整准入和租约释放摘要；澄清脚本副作用、Job 网关适配器及 worker 领取前退出，核对文件媒体与 TTS 组件摘要、部署及架构决策职责，标识历史审计入口和 UI/UX 研究时点；其他条目的职责与证据待核对 |
| `docs/agent-guide.md` | 已核对 | 全文对照根规则、五个项目技能与交付 SOP；49 个链接的本地目标均存在，外部参考保留标注的历史核验时间 |
| `docs/agent-verification.md` | 已核对 | 对照 Make 目标、验证脚本、工具链声明、CI 指南和 Workbench PG 探针清理路径；核对服务前置、副作用与 verify 覆盖限制，未启动服务或执行集成测试 |
| `docs/architecture/README.md` | 已核对 | 逐项核对 23 个本地链接及职责描述；不代表所链接正文已审 |
| `docs/architecture/contract-ownership.md` | 已核对 | 逐项对照机器矩阵的 18 端口写入范围和 7 类查询、私有表及授权来源；更新消费者、运行控制装配与用量消费状态，未重跑集成测试 |
| `docs/architecture/coordination-transactions.md` | 部分核对 | 对照事务契约、生产准入与预算规则、授权、网关清理及集成测试说明；更新同步链路接线，补充 TTS 前置锁并区分测试设计和实际结果；其余锁序、迁移及验证细节仍待核对 |
| `docs/architecture/data-deletion.md` | 部分核对 | 全文阅读并核对删除契约、确认及原键重放、工作台项目快照、跨项目副本引用与未决写入阻断、schema 版本、启动重放和行/对象清理完成投影；澄清查询状态、前缀扫描轮数与数量上限和历史对象版本清理的限制；对照底座保留清理及主动删除缺口，明确工作台项目写入屏障不证明 Speech 源删除与复制的跨模块竞态已解决；未执行删除或恢复演练 |
| `docs/architecture/deployment-units.md` | 部分核对 | 核对 Compose 服务与 binding 停用 profile、内部 HTTP 与对象存储凭据、Web 代理路由与 TLS、镜像及依赖、开发脚本平台限制、主要进程入口、Job 派发失败关闭、媒体检查、TTS 结算、网关本地锁、Controller SQLite、文件 ASR 对象版本及工作台后台循环和入口；澄清导出在途任务恢复缺口及历史 G5 名称与现行发布资格的关系；对照 ADR 0011、入口声明与拓扑示意；31 个本地链接目标存在，其余资格未逐项核实 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/README.md` | 已核对 | 对照构图与校验脚本、ADR、模型清单和实例契约；补充实时常驻前提并重新渲染、检查两图；图示为目标拓扑，不证明实际部署资格 |
| `docs/architecture/distributed-coordination-schema.md` | 已核对 | 全文对照草案 DDL、表及复合外键、预算约束、双库授权生成器和测试前置；澄清正文清除、TEXT 映射、运行权限及测试证据边界；仅收集旧 PG 回归，未运行数据库测试或验证生产部署 |
| `docs/architecture/execution-command.md` | 部分核对 | 核对提交摘要与规范数值、重放、文件 ASR 输入版本读取及 workspace 检查、文本流事件顺序与完成语义、文件 ASR/TTS 消费条件、音色字段省略差异、词表来源、diarization 数量及输出预算约束；对照契约与网关计算，现有整数毫秒及 16/24/48 kHz 采样率下输出样本上限的向上与向下取整结果一致；区分文件与流输入、其他命令对象校验的目标、文本流目标与当前完整文本消费、codec 对象正文定义、独立结构校验与跨对象一致性边界；已核对声音设计、克隆、验证、对齐、分人、VAD 和 codec 的 schema 字段，其他 adapter 边界待核对 |
| `docs/architecture/openai-compatibility.md` | 部分核对 | 核对 SDK 版本、实时装配与用量、REST 字段与幂等拒绝、响应格式、公共及内部控制错误字段、SDK 测试样例、版本字段、实时更新结构及 SpeechRail 固定来源；说明 SDK 示例的地址、模型和音色需按部署替换，记录未知字段的契约与实现错误码差异，对照工作台路径、实时事件命名空间及兼容检查脚本的选择范围；其余扩展语义待核对 |
| `docs/architecture/runtime-governance.md` | 部分核对 | 全文阅读并核对 Asset 检查与 TTL、Job 派发及公开路由、binding 发布与资格核验、实时入口、输出区间计量及调度装配边界；补充现行容量清单和公开能力契约的回收、排队字段及 Job 请求 schema 的策略比较限制，明确 Asset 默认 TTL 变更不追溯、Job / 工作台引用写入缺口及同步执行私有数据的持久化边界；协调及其他保留细节仍待逐项核对 |
| `docs/architecture/speech-api.md` | 部分核对 | 核对实时入口的契约目标与当前装配边界、会话过期配置、部分实时事件字段、同步 TTS 音频交付、列表分页、TTS Job 绑定及成功事件校验边界；澄清预设发布、实时关联字段、会话终态引用、采样区间、只读失败与执行结果未知的重试区别及限制 schema 的覆盖范围，并补充调用示例的部署前提；12 个 HTTP 示例和 4 个实时 TTS 事件示例通过 schema 校验，其余语义待核对 |
| `docs/architecture/speechrail-reuse.md` | 已核对 | 全文按复用政策核对固定来源、路径、许可、领域规则和平台边界；补核上游会议字幕设计与 MCP 代理，确认矩阵是设计方向而非实现声明；未将上游设计或历史测试当作本项目验收证据 |
| `docs/architecture/technical-blueprint.md` | 部分核对 | 局部对照代码、契约或历史证据；修正现行机器契约的时态、Python 契约生成类型、文件 ASR 对象版本边界，区分目标交叠合并与当前逐 VAD 段汇总；核对迁移互斥、工作台启动只读预检及 LLM 配置与传输限制；其余内容待核对 |
| `docs/archive/legacy-postgresql-coordination.md` | 历史保留 | 全文阅读，明确标为旧原型并指向现行架构；无需按当前实现改写，原始证据未逐项复核 |
| `docs/audits/2026-10-22-development-environment-readiness.md` | 历史保留 | 2026-10-22 环境就绪快照；目录入口已说明不代表当前主机状态，未重新验证当时证据 |
| `docs/audits/2026-10-22-speechnexus-design-review.md` | 历史保留 | 0.3.0 设计基线的审查与裁决记录；入口按审查记录引用，未重新验证当时证据 |
| `docs/audits/2026-10-23-architecture-contract-compliance.md` | 历史保留 | 审查快照已注明时点和当前入口；原审查证据未逐项复核 |
| `docs/audits/2026-10-23-architecture-contract-post-refactor-review.md` | 历史保留 | 固定基线的重构后审查，已注明当前入口；原审查证据未逐项复核 |
| `docs/audits/2026-10-23-architecture-contract-repair-closure.md` | 历史保留 | 固定基线的修复验收记录，已注明当前入口；原验收证据未逐项复核 |
| `docs/audits/2026-10-23-ui-ux-design-framework-audit.md` | 历史保留 | 全文阅读，补充绝对化无障碍和门禁评级的证据限制及后续复跑入口；未重跑历史验证 |
| `docs/audits/2026-10-24-b00-megacube-environment-and-capability-gap.md` | 历史保留 | 全文阅读，固定时点的环境前检与首次 ASR 探针；补充 TTS 在线服务判断的后续纠正，未重跑实验 |
| `docs/audits/2026-10-24-b01-default-recovery-driver.md` | 历史保留 | 全文阅读，固定集成基线的默认恢复驱动与进程验证；组件入口引用其当时实测，未重跑测试 |
| `docs/audits/2026-10-24-b01-recovery-progress.md` | 历史保留 | 全文阅读，固定基线的恢复扫描公平性和计数回归；已链接后续驱动验证，未重跑测试 |
| `docs/audits/2026-10-24-b02-controller-container-lifecycle-probe.md` | 历史保留 | 全文阅读，合成容器的生命周期与停止证据；入口按历史探针引用，未重跑实验 |
| `docs/audits/2026-10-24-b02-controller-http-shell-probe.md` | 历史保留 | 全文阅读，固定基线的控制器进程、重启和持久意图探针；已标明后续语义变化，未重跑实验 |
| `docs/audits/2026-10-24-b02-real-asr-controller-probe.md` | 历史保留 | 全文阅读，固定基线的真实 ASR 控制器生命周期探针；入口保留合成输入和资格边界，未重跑实验 |
| `docs/audits/2026-10-24-b02-reconcile-proof-source.md` | 历史保留 | 全文阅读，固定基线的快照证据来源与回归记录；组件文档作为历史证据引用，未重跑测试 |
| `docs/audits/2026-10-24-b02-stop-recovery-probe.md` | 历史保留 | 全文阅读，固定版本的合成容器停止崩溃与恢复探针；后续变化另有引用，未重跑实验 |
| `docs/audits/2026-10-24-b02-trusted-node-stop-probe.md` | 历史保留 | 全文阅读，固定基线的节点停止事实与网关消费分层证据；组件文档按实验范围引用，未重跑测试 |
| `docs/audits/2026-10-24-b02-warmup-gate-probe.md` | 历史保留 | 全文阅读，固定镜像和合成音频的真实 ASR 预热探针；当前组件入口标明单次观测边界，未重跑实验 |
| `docs/audits/2026-10-24-b03-bounded-audio-probe.md` | 历史保留 | 全文阅读，固定基线的媒体解码和 VAD 实验；入口明确不代表识别质量，未重跑探针 |
| `docs/audits/2026-10-24-b03-file-asr-probe.md` | 历史保留 | 全文阅读，固定模型和基线的文件识别及长窗口测量；已链接后续实验，未重跑探针 |
| `docs/audits/2026-10-24-b03-frozen-inputs.md` | 历史保留 | 全文阅读，固定基线的输入接入与 PR 审查记录；当前接口由组件文档承接，未重验当时证据 |
| `docs/audits/2026-10-24-b03-native-asr-scope.md` | 历史保留 | 全文阅读，固定版本与基线的原生清理探针和审查记录；未重验当时证据 |
| `docs/audits/2026-10-24-b03-tts-adapter-probe.md` | 历史保留 | 全文阅读，固定镜像的 CustomVoice 适配器和 ASR 回环实验；索引限定实验范围，未重跑探针 |
| `docs/audits/2026-10-24-b03-tts-engine-feasibility.md` | 历史保留 | 全文阅读，固定镜像的引擎可行性与早期误判纠正；记录时点的后续计划不代表当前状态，未重跑实验 |
| `docs/audits/2026-10-24-b06-cpu-vad-probe.md` | 历史保留 | 全文阅读，固定模型和基线的 CPU 合成探针；组件入口作为历史证据引用，未重跑探针 |
| `docs/audits/2026-10-24-ci-efficiency.md` | 历史保留 | 全文阅读，各轮固定基线的 CI 调研和测量；文首指向现行 CI 规则，未复核历史远端运行 |
| `docs/audits/2026-10-25-b03-default-real-asr-probe.md` | 历史保留 | 全文阅读，固定候选的默认网关 ASR 隔离实验；入口注明私有结果和未覆盖的公开链，未重跑实验 |
| `docs/audits/2026-10-25-b03-real-cancel-settlement.md` | 历史保留 | 全文阅读，固定候选的真实取消与整代次结算实验；保留 partial 和单请求证据缺口，未重跑实验 |
| `docs/audits/2026-10-25-b03-real-stop-settlement.md` | 历史保留 | 全文阅读，固定候选的真实整代次停止与预算结算实验；当前入口标明 fixture 限制，未重跑实验 |
| `docs/audits/2026-10-25-coordination-port-evidence.md` | 历史保留 | 全文阅读，固定提交的协调端口和双库证据快照；当前所有权文档按快照引用，未复核历史 CI |
| `docs/audits/2026-10-25-team-c-next-phase-integration.md` | 历史保留 | 2026-10-25–26 集成与验收记录，已标注时点并指向现行矩阵；原始证据未逐项复核 |
| `docs/audits/2026-10-25-team-c-original-ac-matrix.md` | 历史保留 | 2026-10-25 原 AC 拆解与审阅记录，已标注时点并指向现行矩阵；原始证据未逐项复核 |
| `docs/audits/2026-10-26-b03-default-real-tts-probe.md` | 历史保留 | 全文阅读，固定镜像与旧 v1 配置的私有 TTS 执行实验；当前入口注明版本和范围，未重跑探针 |
| `docs/audits/2026-10-26-b03-real-tts-stop-settlement.md` | 历史保留 | 全文阅读，固定基线的真实整代次停止和租约结算实验；保留 fixture 与未结算 base 的限制，未重跑实验 |
| `docs/audits/2026-10-26-deployment-units-review.md` | 历史保留 | 全文阅读，固定源码基线的部署方案审查；当前规划保留审查入口，未复核历史 issue 与实验 |
| `docs/audits/2026-10-26-project-handoff.md` | 历史保留 | 2026-10-26 三队交接快照，明确不作为后续自动开工指令；原始证据未逐项复核 |
| `docs/audits/2026-10-26-team-b-handoff.md` | 历史保留 | 2026-10-26 团队 B 收尾与交接快照，原始证据未逐项复核 |
| `docs/audits/2026-10-26-team-c-a11y-rerun.md` | 历史保留 | 全文阅读，固定构建的落地页自动可访问性复跑；明确排除读屏及面板状态，未重跑测试 |
| `docs/audits/2026-10-26-team-c-capture-cleanup.md` | 历史保留 | 全文阅读，固定基线的本地采集清理与合成浏览器验证；未重跑测试 |
| `docs/audits/2026-10-26-team-c-closeout-issue-audit.md` | 历史保留 | 全文阅读，指定时间的 issue 状态和交接快照；已提示远端会变化，未复查历史远端数据 |
| `docs/audits/2026-10-26-team-c-evidence-retention.md` | 历史保留 | 全文阅读，证据丢失事故与当时规则；文首已指向现行 SOP，未复核历史归档 |
| `docs/audits/2026-10-26-team-c-input-device.md` | 历史保留 | 全文阅读，固定基线的输入选择及虚拟设备验证；未重跑测试或核验物理设备 |
| `docs/audits/2026-10-26-team-c-operation-coverage.md` | 历史保留 | 全文阅读，固定 head 的工作台操作接线映射；历史交接按当时范围引用，未重跑映射守卫 |
| `docs/audits/2026-10-26-team-c-realtime-focus.md` | 历史保留 | 全文阅读，固定基线的焦点修复、反例及后续合入记录；未重跑测试或复核远端 CI |
| `docs/audits/2026-10-26-team-c-realtime-integrity.md` | 历史保留 | 全文阅读，固定基线的实时发送和本地停止修复及合成验证；未重跑测试 |
| `docs/audits/2026-10-26-team-c-remaining-gaps.md` | 历史保留 | 全文阅读，固定收尾基线的团队 C 缺口及交接条件；由历史交接引用，未将其作为当前进度 |
| `docs/audits/2026-10-26-team-c-render-loop-status.md` | 历史保留 | 全文阅读，固定收尾基线的有声书实现与证据矩阵；由历史交接引用，未重验当时证据 |
| `docs/audits/2026-11-03-controlled-tts-prepare.md` | 历史保留 | 2026-11-03 各窗口验收与清理快照，已区分第六窗口结论和第五窗口历史缺口；各窗口原始事实未逐项重验 |
| `docs/audits/README.md` | 已核对 | 对照选编记录的标题、摘要和证据边界，明确历史 UI/UX 评级限制与选编范围；未逐项重验原始证据 |
| `docs/components/README.md` | 已核对 | 核对索引中各目标文件、标题与候选状态；不代表目标正文已审 |
| `docs/components/audio-asset-inspection.md` | 历史保留 | 2026-10-26 候选与资格记录，保留原时点事实；当前入口的 TextAssetInspector 装配已对照源码，历史证据未重验 |
| `docs/components/binding-defaults-producer.md` | 已核对 | 逐节静态对照配置、契约、创建与固定读取、错误处理、权限生命周期及测试定义；修正 binding 操作数、服务端校验器选择及发布 / 激活状态，未运行服务或重跑集成测试 |
| `docs/components/binding-parameter-defaults.md` | 已核对 | 逐节静态对照快照 schema、共享字段、生产与固定读取、生成消费者和测试定义；最终参数合并仍为独立验收项，未运行服务 |
| `docs/components/controlled-asr-generation.md` | 已核对 | 对照凭据派生、模板和私有描述、终态停止边界及真实整代次实验；保留 fixture 与未验收限制，未运行模型或控制器 |
| `docs/components/cpu-vad.md` | 已核对 | 对照策略、缓冲、CPU 适配器、固定模型声明、文件消费者、Compose 静态配置及测试入口；未运行模型或验证实际部署资格 |
| `docs/components/execution-gateway.md` | 已核对 | 对照装配、提交与屏障、成功终态、恢复、取消、正文清理、观察及输出区间事务和测试定义；未运行集成测试或验证真实引擎 |
| `docs/components/execution-staging-results.md` | 已核对 | 对照回执契约、迁移、列授权、登记 CAS/重放、覆盖检查、HTTP 查询及测试入口；未运行集成测试 |
| `docs/components/file-asr-audio.md` | 已核对 | 对照共享媒体模块、源格式、限制、时钟投影、测试入口及已知消费者装配；未重跑测试或验证部署 |
| `docs/components/file-asr-recognition.md` | 已核对 | 逐节静态对照参数、协议、装配、执行与测试清单；未重新实测 |
| `docs/components/file-asr-results.md` | 已核对 | 逐节静态对照结果聚合、私有对象、命令与相关测试；未运行服务 |
| `docs/components/file-tts.md` | 已核对 | 全文静态对照策略、HTTP、命令、编码器、worker 与历史探针记录；未重跑测试或历史探针 |
| `docs/components/frozen-execution-inputs.md` | 已核对 | 对照命令桥失败分类、对象版本与传输期限、ASR 参数映射及集成测试入口；未运行集成测试或验证部署 |
| `docs/components/gateway-execution-client.md` | 已核对 | 对照客户端配置、认证、结果查询、停止身份、重试、调用方装配及测试入口；未运行测试或验证目标部署 |
| `docs/components/model-binding-drafts.md` | 已核对 | 对照草稿和目录读取授权、生命周期事务、0011 降级迁移、HTTP 源、生成消费者及共享样例；修正锁、期限、事件原因、禁用意图权限、空 head 激活和历史版本表述；未运行集成测试 |
| `docs/components/node-controller.md` | 已核对 | 静态对照模板、容器创建、观察、路由与停止恢复；历史探针按原时点保留，未运行服务 |
| `docs/components/node-stop-recovery.md` | 已核对 | 静态对照检查点、停止恢复、诊断与测试定义；历史探针按原时点保留，未运行服务 |
| `docs/components/production-admission-storage.md` | 已核对 | 静态对照存储、授权、锁序、恢复及公开 Job 原始意图；未运行服务 |
| `docs/components/production-admission.md` | 已核对 | 静态对照配额、锁序、原身份恢复及同步 TTS settlement 重放；未运行服务 |
| `docs/components/production-assets.md` | 已核对 | 静态对照上传、保留、授权、资产与工作区 HTTP 及测试定义；未运行服务 |
| `docs/components/production-jobs.md` | 已核对 | 对照创建、认领、派发、发布、worker 配置和测试入口；说明停止竞态与状态摘要检查限制，未运行集成测试 |
| `docs/components/production-leases.md` | 已核对 | 静态对照隔离与释放事务、停止事实、0028 视图授权及测试定义；未运行服务 |
| `docs/components/production-owners.md` | 已核对 | 静态对照锁序、数据库时间、激活条件、恢复与测试定义；未运行服务 |
| `docs/components/production-runtime-intents.md` | 已核对 | 静态对照迁移、create/observe 锁序与状态转换、重放、原生证据和测试定义；未运行服务 |
| `docs/components/profile-drafts.md` | 已核对 | 静态对照草稿事务、权限、HTTP 处理顺序、配置和测试定义；未运行服务 |
| `docs/components/profile-lifecycle-contract.md` | 已核对 | 静态对照契约、生成入口、路由装配及私有观察边界，区分计划行为与测试定义；未运行服务 |
| `docs/components/profile-lifecycle-intents.md` | 已核对 | 静态对照记录与恢复、并发时限、迁移授权、装配和测试定义；未运行服务 |
| `docs/components/pronunciation-drafts.md` | 已核对 | 逐节静态对照草稿事务、权限、迁移和集成测试定义；区分固定读取与恢复校验，未运行服务 |
| `docs/components/pronunciation-read-contract.md` | 已核对 | 静态对照读取器权限、HTTP 处理顺序、生成消费者和测试定义；未运行服务 |
| `docs/components/public-job-model-binding.md` | 已核对 | 逐节静态核对固定 binding 选择、事务期限、恢复、工作台消费者与测试；未运行服务 |
| `docs/components/repository-layout.md` | 已核对 | 逐节静态对照工作区、依赖门禁、配置、验证入口、界面说明及工程基线；收窄无法全面证明的 UI 描述，未运行服务 |
| `docs/components/runtime-controller-client.md` | 已核对 | 逐节静态对照客户端、节点观察循环、持久投递和测试清单；未运行服务 |
| `docs/components/synchronous-speech.md` | 已核对 | 逐节静态对照 TTS 装配、准入锁序、恢复、结算预检、配置和错误处理；未运行服务 |
| `docs/components/transcript-import-and-text-export.md` | 已核对 | 逐节静态对照来源校验、导入事务、编辑器、下载和公开契约；未运行服务 |
| `docs/components/trusted-node-stop.md` | 已核对 | 逐节静态对照查询契约、节点检查点、网关身份校验、证据派生、恢复扫描和历史验证入口；未运行服务 |
| `docs/components/workbench-content-downloads.md` | 已核对 | 逐节静态对照下载事务、HTTP、对象存储、浏览器消费者及公开契约；未运行服务 |
| `docs/components/workbench-export-integrity.md` | 已核对 | 逐节静态对照导出 worker、媒体校验、配置、组合根、持久写入及测试清单；未运行服务 |
| `docs/components/workbench-voice-catalogue.md` | 已核对 | 逐节静态对照公开契约、适配器、运行配置及 Web 消费者；未运行服务 |
| `docs/components/zero-dispatch-contract-core.md` | 已核对 | 对照候选 schema、身份引用、状态转换、恢复分类、测试定义与当前取消路径；历史交接字节证据未重验 |
| `docs/decisions/0001-product-boundaries-and-authentication-scope.md` | 已核对 | 逐项对照当前 PRD 与产品架构的底座、工作台、身份和硬件边界；ADR 限定产品决定，不代表实现或部署已验收 |
| `docs/decisions/0002-speech-models-engines-and-loading.md` | 已核对 | 对照产品架构的模型分工、加载治理和 PRD 首轮性能口径；历史选型来源未重验，目标部署资格另行取证 |
| `docs/decisions/0003-workbench-llm-network-boundary.md` | 已核对 | 逐项对照 PRD、产品架构和技术方案的默认关闭、内容网络与资源边界，并抽核配置和 HTTP 投影；未实测部署出站限制 |
| `docs/decisions/0004-cluster-ready-architecture-and-single-node-mvp.md` | 已核对 | 对照产品要求、后续协调与部署决定及资源预算边界；限定历史表名和高可用示意，未验证目标部署 |
| `docs/decisions/0005-complete-execution-and-data-lifecycle-contracts.md` | 已核对 | 对照执行与删除设计、同步错误契约、SDK 测试定义和内部停止恢复契约；修正 409 恢复范围，历史验证未重跑 |
| `docs/decisions/0006-cluster-coordination-and-storage-portability.md` | 已核对 | 对照产品要求、协调事务方案、数据库支持、准入与停止证据边界；区分历史计划与当前入口，未运行数据库或集群验证 |
| `docs/decisions/0007-legacy-coordination-retirement.md` | 已核对 | 对照旧 fixture 与测试引用、规则映射和退出条件；保留条件性清理决定，不声称规则迁移或退出已完成 |
| `docs/decisions/0008-runtime-intents-before-controller-observation.md` | 已核对 | 对照未观察快照、outbox 目标、生产事务及持久投递说明；保留决策时消费者顺序，未运行服务或重验历史数据库证据 |
| `docs/decisions/0009-workbench-explicit-binding-http-version.md` | 已核对 | 对照 v1/v2 契约、渲染路由、配置发现及 Web 消费者，修正生成路由状态；未运行真实语音链 |
| `docs/decisions/0010-private-audio-receipt-compatibility.md` | 已核对 | 对照回执类型、区间登记事务、独立查询投影和同步合成消费者；保留历史交付边界，未重跑集成测试 |
| `docs/decisions/0011-distributed-deployment-and-platform-boundaries.md` | 已核对 | 对照产品要求、部署单元与平台职责、事务和验收边界；区分历史关口与当前入口，未独立验证平台运行 |
| `docs/decisions/0012-public-job-surface.md` | 已核对 | 对照公开 Job 状态机、幂等、准入、派发及工作台消费者；修正状态转换并补充固定绑定未传递的限制，未运行服务 |
| `docs/decisions/0013-binding-lifecycle-admin-http.md` | 已核对 | 对照管理契约、激活 CAS、原键重放、冲突提示及现行操作指南；保留拟议状态，历史审查和远端验收未重验 |
| `docs/decisions/README.md` | 已核对 | 对照 0001–0013 的标题、日期与 frontmatter 状态；明确拟议 ADR 不等于已接受，不代表各决策正文已审完 |
| `docs/design/ui-ux-design-framework-and-tokens.md` | 部分核对 | 全文阅读，对照 CI 路由与独立 Token 门禁、基础及语义 Token、主题选择和品牌色、共享外壳、诊断三态映射、文案及静态扫描范围；记录状态标签与目标的差距，修正 CI 路由图及 lint 覆盖范围，核对共享无障碍样式的覆盖范围；其他细节待核对 |
| `docs/development-environment.md` | 已核对 | 对照锁定版本、初始化与服务脚本、检查和冒烟入口；修正安装、权限及端口边界，未实测参考宿主运行状态 |
| `docs/operations/README.md` | 已核对 | 核对目录定位、索引说明及所有本地链接目标；历史快照已标识 |
| `docs/operations/agent-delivery-sop.md` | 已核对 | 全文对照根规约、军团协作和验证指南入口，核对 7 个本地链接；平台能力、部署与运行状态须按任务实测 |
| `docs/operations/backfill-binding-publication-evidence.md` | 已核对 | 对照 CLI、映射约束与去重计数、回填事务、授权声明及 0021 回退锁；未连接生产库或运行回填 |
| `docs/operations/binding-activation-verification.md` | 已核对 | 对照 PG 并发矩阵、跨操作 ASGI 测试与故障清理 fixture；澄清测试设计不等于实际运行通过，未运行 PG |
| `docs/operations/binding-disable-worker.md` | 已核对 | 对照配置范围、角色授权列、URL 策略、处理循环、异常分支及原租约结算入口；未连接部署或执行停用 |
| `docs/operations/binding-lifecycle.md` | 已核对 | 对照授权、生命周期事务、幂等重放、并发矩阵与恢复测试；澄清连接中断可能无响应，未运行 PG 或部署 |
| `docs/operations/builtin-voice-qualification.md` | 已核对 | 对照资格入口、授权、退出分支、制品脚本摘要、probe 音频与终态验证及受控窗口；未运行探针或核验部署权限 |
| `docs/operations/capacity-manifest.md` | 已核对 | 对照 schema、校验器、账本、准入错误、样例及文件摘要；澄清 VAD、发布门禁、fixture、查询隔离及样例到期边界，未连接数据库或重跑模型 |
| `docs/operations/compose-minimal.md` | 已核对 | 对照 Compose、迁移、角色授权、探针、注册及停用恢复路径；修正权限、失败边界和探针路径，未执行部署 |
| `docs/operations/file-asr-binding-handoff.md` | 已核对 | 对照固定制品、绑定定义、离线工具、命令消费及交接测试，补充后续旧入口屏障联验证据及故障注入限制；未重跑实验或测试 |
| `docs/operations/file-asr-runtime.md` | 已核对 | 对照配置、双 lane 共享限制、对象版本、worker 取消、结果恢复及停止联验；修正集成测试的停止证据描述，未运行测试或部署 |
| `docs/operations/file-tts-binding-handoff.md` | 已核对 | 对照固定制品与运行清单、绑定定义、离线工具、注册与命令消费测试及后续 SDK 和租约结算证据；保留历史实验结果，未重跑测试或模型 |
| `docs/operations/file-tts-runtime.md` | 已核对 | 对照配置、双 lane 共享 worker、关闭边界、音频上限、测试及真实停止和结算证据；明确组件集成测试范围，未运行测试或部署 |
| `docs/operations/local-end-to-end.md` | 历史保留 | 2026-10-28 指定基线的隔离装配与失败记录；已提示旧发布命令不适用于当前版本，现行入口按历史证据引用，原始输出未逐项复核 |
| `docs/operations/paperclip/environment-readiness.md` | 历史保留 | 已明确标注为旧环境与部署快照，当前流程指向交付 SOP；原始证据未逐项复核 |
| `docs/operations/paperclip/roles/common.md` | 已核对 | 全文对照军团工作流与交付 SOP；这是岗位共用规范，不证明实际部署或权限 |
| `docs/operations/paperclip/roles/evidence.md` | 已核对 | 全文对照岗位与证据边界；这是职责规范，不证明部署配置或证据原件 |
| `docs/operations/paperclip/roles/execution.md` | 已核对 | 全文对照军团工作流、执行与交接边界；实际部署配置及权限按任务核实 |
| `docs/operations/paperclip/roles/manager.md` | 已核对 | 全文对照军团工作流、授权与交付 SOP；实际岗位及权限按任务核实 |
| `docs/operations/paperclip/roles/platform.md` | 已核对 | 全文对照军团工作流、契约与交接边界；实际部署配置及权限按任务核实 |
| `docs/operations/paperclip/roles/qa.md` | 已核对 | 全文对照军团工作流和验收证据边界；实际岗位及权限按任务核实 |
| `docs/operations/paperclip/roles/reviewer.md` | 已核对 | 全文对照军团工作流和独立审查边界；实际部署配置及权限按任务核实 |
| `docs/operations/paperclip/roles/workbench.md` | 已核对 | 全文对照军团工作流和工作台职责边界；实际部署及权限按任务核实 |
| `docs/operations/paperclip/runtime-incidents.md` | 历史保留 | 历史故障、处置与后续证伪记录；已限定旧操作不构成当前授权，并指向现行交付 SOP；事故原件未逐项核验 |
| `docs/operations/paperclip.md` | 已核对 | 全文对照交付 SOP、根规则及岗位正文，明确已取消排期的 issue 与整改计划为历史入口，补充三机分工和传输路径的时点；实际平台配置、身份及环境可用性须在执行前核验，本次未连接或修改平台 |
| `docs/operations/production-coordination-storage.md` | 已核对 | 对照 0002 表与索引、迁移链、列级授权、组合约束及释放守卫；区分基础约束与受控事务职责，未运行数据库测试 |
| `docs/operations/production-runtime-delivery.md` | 已核对 | 对照配置、租约、unknown 路径、迁移、授权、ACK、Compose 及测试入口；未执行集成测试或验证目标部署 |
| `docs/operations/publish-binding-revision.md` | 已核对 | 对照 CLI 观察、退出分类、发布事务的目标校验、草稿与证据检查、已有事实和原意图重放；未连接数据库或验证部署切换 |
| `docs/operations/qualification-report.md` | 已核对 | 对照报告契约、样例状态和摘要、离线校验器及产品只读组件；澄清发布与 fixture 边界并修正内部 VAD 描述，未重跑测量或连接数据库 |
| `docs/operations/reconciliation-recovery.md` | 已核对 | 对照 unknown 停止规则、abort 恢复入口、租约释放边界及只读查询字段与回执契约；历史快照保留原时点，未连接数据库执行查询 |
| `docs/operations/register-engine.md` | 已核对 | 对照命令参数、健康探测、注册事务、同代重放、readiness 刷新、投递阻断与验证查询；未连接数据库或注册实例 |
| `docs/operations/release-preflight.md` | 已核对 | 对照脚本检查入口、配置加载和 C0–C12 判断；修正版本、身份、临时文件、资格场景、Compose 文本匹配及固定快照边界，未执行会访问服务或写入的检查 |
| `docs/operations/release-readiness.md` | 历史保留 | 全文阅读，固定候选与核验时点的发布就绪快照；运维索引标为历史，未逐项复核历史证据 |
| `docs/operations/synchronous-transcription.md` | 已核对 | 对照配置、配方、请求校验、媒体探测、准入与网关提交轮询、结果投影和错误映射；未运行集成测试或验证真实引擎 |
| `docs/plans/2026-10-22-speechnexus-implementation-plan.md` | 历史保留 | 全文阅读并核对引用入口；保留原阶段和预算假设，标明当前交付入口，未逐项复核历史证据 |
| `docs/plans/2026-10-23-agent-team-startup-briefs.md` | 历史保留 | 原首轮派工文本，文首及索引注明历史性质；未逐项复核原始证据 |
| `docs/plans/2026-10-23-architecture-contract-repairs.md` | 历史保留 | 固定审查基线的修复验收记录；未逐项复核历史验证证据 |
| `docs/plans/2026-10-23-cluster-coordination-refactor.md` | 历史保留 | 原重构计划已注明历史性质；修正当前架构入口的引用，未逐项复核历史证据 |
| `docs/plans/2026-10-23-implementation-tracking.md` | 历史保留 | 2026-10-26 状态快照，已指向当前需求矩阵；未重新核验原远端状态 |
| `docs/plans/2026-10-23-team-a-goal.md` | 历史保留 | 已替代的团队目标及收尾交接；未逐项复核历史证据 |
| `docs/plans/2026-10-23-team-b-goal.md` | 历史保留 | 原团队目标及收尾交接，已明确不作当前排期；未逐项复核历史证据 |
| `docs/plans/2026-10-23-three-team-delivery-plan.md` | 历史保留 | 固定基线的三团队分工、任务和关口规划；文首与计划索引已注明历史性质和当前入口，历史验收证据未逐项复核 |
| `docs/plans/2026-10-24-a01-runtime-intent-unit.md` | 历史保留 | 全文阅读，标明原实施路线的时点并修正组件文档入口；未重跑历史验证 |
| `docs/plans/2026-10-24-a02-asset-publication-unit.md` | 历史保留 | 全文阅读，标明原实施路线与分批状态的时点；未重跑历史验证 |
| `docs/plans/2026-10-24-a02-binding-drafts-unit.md` | 历史保留 | 全文阅读，标明原实施计划与验证结果的时点；未重跑历史验证 |
| `docs/plans/2026-10-24-b01-default-recovery-driver.md` | 历史保留 | 全文阅读并标明原方案时点；未重跑历史验证 |
| `docs/plans/2026-10-24-b03-frozen-inputs.md` | 历史保留 | 全文阅读并标明原接入范围和验收时点；未重跑历史验证 |
| `docs/plans/2026-10-24-b03-native-asr-scope.md` | 历史保留 | 全文阅读并标明原实现和后续安排时点；未重跑历史验证 |
| `docs/plans/2026-10-24-team-c-2874-integration-handoff.md` | 历史保留 | 全文阅读，固定基线和日期的集成交接；计划索引已说明历史材料范围，未重跑验证 |
| `docs/plans/2026-10-24-team-c-7cd-integration-handoff.md` | 历史保留 | 全文阅读，固定基线和日期的集成交接；计划索引已说明历史材料范围，未重跑验证 |
| `docs/plans/2026-10-24-team-c-9ee-integration-handoff.md` | 历史保留 | 全文阅读，固定基线和日期的集成交接；计划索引已说明历史材料范围，未重跑验证 |
| `docs/plans/2026-10-24-team-c-a98-integration-handoff.md` | 历史保留 | 全文阅读，固定日期和提交的集成交接；索引及后续目标明确其时点，未重跑验证 |
| `docs/plans/2026-10-24-team-c-c00-c1-handoff.md` | 历史保留 | 全文阅读，固定日期和提交的消费交接；未重跑历史验证 |
| `docs/plans/2026-10-24-team-c-c00-c1-plan.md` | 历史保留 | 全文阅读，与固定基线交接配对的实施计划；不作为现行执行指令，未重跑验证 |
| `docs/plans/2026-10-24-team-c-c00-deletion-handoff.md` | 历史保留 | 全文阅读，修正交接状态并保留原时点事实；未重跑历史验证 |
| `docs/plans/2026-10-24-team-c-c00-http-handoff.md` | 历史保留 | 全文阅读，固定日期和基线的 HTTP 交接；未重跑历史验证 |
| `docs/plans/2026-10-24-team-c-c00-http-plan.md` | 历史保留 | 全文阅读，配套固定基线的实施计划；不作为现行执行指令，未重跑验证 |
| `docs/plans/2026-10-24-team-c-c00-listing-handoff.md` | 历史保留 | 全文阅读，修正交接状态并保留原基线及验证；未重跑历史验证 |
| `docs/plans/2026-10-24-team-c-c00-listing-plan.md` | 历史保留 | 全文阅读，修正计划状态并保留原实施记录；未重跑历史验证 |
| `docs/plans/2026-10-24-team-c-c00-postgres-handoff.md` | 历史保留 | 全文阅读，修正交接状态并保留原时点事实；未重跑历史验证 |
| `docs/plans/2026-10-24-team-c-c00-project-boundary-handoff.md` | 历史保留 | 全文阅读，修正交接状态并保留原时点事实；未重跑历史验证 |
| `docs/plans/2026-10-24-team-c-c00-text-handoff.md` | 历史保留 | 全文阅读，已有历史时点说明并修正状态；未重跑历史验证 |
| `docs/plans/2026-10-24-team-c-c00-version-handoff.md` | 历史保留 | 全文阅读，已有原时点与后续交接说明并修正状态；未重跑历史验证 |
| `docs/plans/2026-10-24-team-c-c01-events-handoff.md` | 历史保留 | 全文阅读，修正交接状态并保留原时点验证；未重跑历史验证 |
| `docs/plans/2026-10-24-team-c-c01-events-plan.md` | 历史保留 | 全文阅读，与固定基线交接配对的原实施计划；未重跑验证 |
| `docs/plans/2026-10-24-team-c-c01-recovery-handoff.md` | 历史保留 | 全文阅读，修正交接状态并保留原时点验证；未重跑历史验证 |
| `docs/plans/2026-10-24-team-c-c01-recovery-plan.md` | 历史保留 | 全文阅读，配对固定基线交接的原实施计划；未重跑验证 |
| `docs/plans/2026-10-24-team-c-c01-task-center-handoff.md` | 历史保留 | 全文阅读，修正交接状态并保留原时点验证；未重跑历史验证 |
| `docs/plans/2026-10-24-team-c-c01-task-center-plan.md` | 历史保留 | 全文阅读，修正计划状态并保留原实施记录；未重跑历史验证 |
| `docs/plans/2026-10-24-team-c-c02-asset-reconcile-handoff.md` | 历史保留 | 全文阅读，已有时点说明并修正状态；未重跑历史验证 |
| `docs/plans/2026-10-24-team-c-c02-asset-reconcile-plan.md` | 历史保留 | 全文阅读，与固定基线交接配对的原实施计划；未重跑验证 |
| `docs/plans/2026-10-24-team-c-c02-download-contract-handoff.md` | 历史保留 | 全文阅读，候选契约已注明未成为正式路径并指向当前导出说明；未重跑历史验证 |
| `docs/plans/2026-10-24-team-c-c02-download-contract-plan.md` | 历史保留 | 全文阅读，配对候选契约交接的原计划；未重跑验证 |
| `docs/plans/2026-10-24-team-c-c02-file-flow-handoff.md` | 历史保留 | 全文阅读，修正交接状态并保留原时点验证；未重跑历史验证 |
| `docs/plans/2026-10-24-team-c-c02-host-handoff.md` | 历史保留 | 全文阅读，已有时点说明并修正状态；未重跑历史验证 |
| `docs/plans/2026-10-24-team-c-c02-host-plan.md` | 历史保留 | 全文阅读，与固定基线交接配对的原实施计划；未重跑验证 |
| `docs/plans/2026-10-24-team-c-c02-recovery-handoff.md` | 历史保留 | 全文阅读，修正交接状态并保留原时点验证；未重跑历史验证 |
| `docs/plans/2026-10-24-team-c-c02-recovery-plan.md` | 历史保留 | 全文阅读，与固定基线交接配对的原实施计划；未重跑验证 |
| `docs/plans/2026-10-24-team-c-c02-text-export-handoff.md` | 历史保留 | 全文阅读，修正历史交接状态；历史测试未重跑 |
| `docs/plans/2026-10-24-team-c-c02-text-export-plan.md` | 历史保留 | 全文阅读，保留原基线计划；历史验证未重跑 |
| `docs/plans/2026-10-24-team-c-c02-transcript-import-handoff.md` | 历史保留 | 全文阅读，标明历史交接与当前入口；历史验证未重跑 |
| `docs/plans/2026-10-24-team-c-c02-transcript-import-plan.md` | 历史保留 | 全文阅读，修正原基线计划状态；历史验证未重跑 |
| `docs/plans/2026-10-24-team-c-c02-tts-handoff.md` | 历史保留 | 全文阅读，标明历史交接与当前入口；历史验证未重跑 |
| `docs/plans/2026-10-24-team-c-c02-tts-host-handoff.md` | 历史保留 | 全文阅读，标明历史交接与当前入口；历史验证未重跑 |
| `docs/plans/2026-10-24-team-c-c02-tts-host-plan.md` | 历史保留 | 全文阅读，保留原基线计划；历史验证未重跑 |
| `docs/plans/2026-10-24-team-c-c02-tts-plan.md` | 历史保留 | 全文阅读，保留原基线计划；历史验证未重跑 |
| `docs/plans/2026-10-24-team-c-c02-upload-recovery-handoff.md` | 历史保留 | 全文阅读，标明历史交接与当前入口；历史验证未重跑 |
| `docs/plans/2026-10-24-team-c-c02-upload-recovery-plan.md` | 历史保留 | 全文阅读，保留原基线计划；历史验证未重跑 |
| `docs/plans/2026-10-24-team-c-c04-capture-handoff.md` | 历史保留 | 全文阅读，修正历史交接状态；历史验证未重跑 |
| `docs/plans/2026-10-24-team-c-c04-capture-plan.md` | 历史保留 | 全文阅读，修正原基线计划状态；历史验证未重跑 |
| `docs/plans/2026-10-24-team-c-c04-media-handoff.md` | 历史保留 | 全文阅读，修正历史交接状态；历史验证未重跑 |
| `docs/plans/2026-10-24-team-c-c05-prompter-handoff.md` | 历史保留 | 全文阅读，已有原时点与后续交接说明；历史验证未重跑 |
| `docs/plans/2026-10-24-team-c-c05-prompter-plan.md` | 历史保留 | 全文阅读，保留原基线计划；历史验证未重跑 |
| `docs/plans/2026-10-24-team-c-c05-seal-handoff.md` | 历史保留 | 全文阅读，已有原时点与后续交接说明；历史验证未重跑 |
| `docs/plans/2026-10-24-team-c-c05-seal-plan.md` | 历史保留 | 全文阅读，保留原基线计划；历史验证未重跑 |
| `docs/plans/2026-10-24-team-c-c06-llm-handoff.md` | 历史保留 | 全文阅读，已有原时点及后续交接说明；历史验证未重跑 |
| `docs/plans/2026-10-24-team-c-c06-llm-plan.md` | 历史保留 | 全文阅读，保留原基线计划及当时阻塞；历史验证未重跑 |
| `docs/plans/2026-10-24-team-c-c06-tasks-handoff.md` | 历史保留 | 全文阅读，已有原基线与后续交接说明；历史验证未重跑 |
| `docs/plans/2026-10-24-team-c-c06-tasks-plan.md` | 历史保留 | 全文阅读，保留原基线计划；历史验证未重跑 |
| `docs/plans/2026-10-24-team-c-c07-render-handoff.md` | 历史保留 | 全文阅读，已有原时点及后续交接说明；历史验证未重跑 |
| `docs/plans/2026-10-24-team-c-c07-render-plan.md` | 历史保留 | 全文阅读，保留原基线计划；历史验证未重跑 |
| `docs/plans/2026-10-24-team-c-c1-editor-handoff.md` | 历史保留 | 全文阅读，标明历史交接与当前入口；历史验证未重跑 |
| `docs/plans/2026-10-24-team-c-c1-editor-plan.md` | 历史保留 | 全文阅读，保留原基线计划；历史验证未重跑 |
| `docs/plans/2026-10-24-team-c-c1-management-handoff.md` | 历史保留 | 全文阅读，标明历史交接与当前入口；历史验证未重跑 |
| `docs/plans/2026-10-24-team-c-c1-management-plan.md` | 历史保留 | 全文阅读，保留原基线计划；历史验证未重跑 |
| `docs/plans/2026-10-24-team-c-deletion-ui-handoff.md` | 历史保留 | 全文阅读，标明历史交接与当前入口；历史验证未重跑 |
| `docs/plans/2026-10-24-team-c-deletion-ui-plan.md` | 历史保留 | 全文阅读，保留原基线计划；历史验证未重跑 |
| `docs/plans/2026-10-24-team-c-eb299-integration-handoff.md` | 历史保留 | 全文阅读，标明历史集成与当前入口；历史验证未重跑 |
| `docs/plans/2026-10-24-team-c-goal.md` | 历史保留 | 分段全文阅读，明确最终交接时点和当前入口；各轮历史验证未重跑 |
| `docs/plans/2026-10-24-team-c-meeting-http-handoff.md` | 历史保留 | 全文阅读，标明历史交接与当前入口；历史验证未重跑 |
| `docs/plans/2026-10-24-team-c-meeting-http-plan.md` | 历史保留 | 全文阅读，保留原基线计划；历史验证未重跑 |
| `docs/plans/2026-10-24-team-c-meeting-ui-handoff.md` | 历史保留 | 全文阅读，标明历史交接与当前入口；历史验证未重跑 |
| `docs/plans/2026-10-24-team-c-meeting-ui-plan.md` | 历史保留 | 全文阅读，保留原基线计划；历史验证未重跑 |
| `docs/plans/2026-10-24-team-c-realtime-asr-handoff.md` | 历史保留 | 全文阅读，标明历史交接与当前入口；历史验证未重跑 |
| `docs/plans/2026-10-24-team-c-realtime-asr-plan.md` | 历史保留 | 全文阅读，保留原基线计划；历史验证未重跑 |
| `docs/plans/2026-10-24-team-c-realtime-tts-handoff.md` | 历史保留 | 全文阅读，标明历史交接与当前入口；历史验证未重跑 |
| `docs/plans/2026-10-24-team-c-realtime-tts-plan.md` | 历史保留 | 全文阅读，保留原基线计划；历史验证未重跑 |
| `docs/plans/2026-10-24-team-c-runtime-root-handoff.md` | 历史保留 | 全文阅读，标明旧配置与命令时点及现行部署入口；历史验证未重跑 |
| `docs/plans/2026-10-24-team-c-runtime-root-plan.md` | 历史保留 | 全文阅读，保留原基线计划；历史验证未重跑 |
| `docs/plans/2026-10-24-team-c-teleprompter-persistence-handoff.md` | 历史保留 | 全文阅读，标明历史命令、交接与当前入口；历史验证未重跑 |
| `docs/plans/2026-10-24-team-c-teleprompter-persistence-plan.md` | 历史保留 | 全文阅读，保留原基线计划；历史验证未重跑 |
| `docs/plans/2026-10-24-team-c-voice-create-handoff.md` | 历史保留 | 全文阅读，标明历史交接与当前入口；历史验证未重跑 |
| `docs/plans/2026-10-24-team-c-voice-create-plan.md` | 历史保留 | 全文阅读，保留原基线计划；历史验证未重跑 |
| `docs/plans/2026-10-24-team-c-workspaces-live-handoff.md` | 历史保留 | 全文阅读，标明历史联验与当前入口；历史验证未重跑 |
| `docs/plans/2026-10-24-team-c-workspaces-live-plan.md` | 历史保留 | 全文阅读，保留原基线计划；历史验证未重跑 |
| `docs/plans/2026-10-24-team-c-workspaces-read-handoff.md` | 历史保留 | 全文阅读，标明历史交接与当前入口；历史验证未重跑 |
| `docs/plans/2026-10-24-team-c-workspaces-read-plan.md` | 历史保留 | 全文阅读，保留原基线计划；历史验证未重跑 |
| `docs/plans/2026-10-25-b03-durable-file-runner.md` | 历史保留 | 全文阅读，标明原实施范围与当前入口；未逐项复验执行证据 |
| `docs/plans/2026-10-25-b03-worker-fence.md` | 历史保留 | 全文阅读，标明原单元状态与当前入口；历史验证未重跑 |
| `docs/plans/2026-10-25-ci-path-routing.md` | 历史保留 | 全文阅读，现行维护说明见 validation/ci-validation.md；历史 CI 未重跑 |
| `docs/plans/2026-10-25-design-token-gate.md` | 历史保留 | 全文阅读，保留原门禁计划及验证；现行说明见 validation/ci-validation.md |
| `docs/plans/2026-10-25-raster-gradient-contrast.md` | 历史保留 | 全文阅读，保留原计划及验证；现行说明见 validation/ci-validation.md |
| `docs/plans/2026-10-25-team-a-realignment.md` | 历史保留 | 全文阅读，修正历史复盘状态并指向当前矩阵；历史验证未重跑 |
| `docs/plans/2026-10-25-team-c-admin-diagnostics-handoff.md` | 历史保留 | 全文阅读，修正历史状态并指向当前入口；历史验证未重跑 |
| `docs/plans/2026-10-25-team-c-assistant-honest-consumer-handoff.md` | 历史保留 | 全文阅读，标明历史状态及后续浏览器证据；历史验证未重跑 |
| `docs/plans/2026-10-25-team-c-assistant-honest-state-browser-handoff.md` | 历史保留 | 全文阅读，修正历史状态并指向当前入口；历史验证未重跑 |
| `docs/plans/2026-10-25-team-c-authoring-consumer-fix-handoff.md` | 历史保留 | 全文阅读，标明历史状态与当前入口；历史验证未重跑 |
| `docs/plans/2026-10-25-team-c-capability-report-handoff.md` | 历史保留 | 全文阅读，标明历史状态与当前入口；历史验证未重跑 |
| `docs/plans/2026-10-25-team-c-chapter-create-consumer-handoff.md` | 历史保留 | 全文阅读，标明历史清点与后续交接；历史验证未重跑 |
| `docs/plans/2026-10-25-team-c-export-production-prewiring-handoff.md` | 历史保留 | 全文阅读，标明历史状态与当前入口；历史验证未重跑 |
| `docs/plans/2026-10-25-team-c-issue-four-part-refresh-handoff.md` | 历史保留 | 全文阅读，区分历史 issue 回读与当前状态；未查询远端 |
| `docs/plans/2026-10-25-team-c-keyboard-narrow-verification-handoff.md` | 历史保留 | 全文阅读，标明历史验证与当前入口；历史验证未重跑 |
| `docs/plans/2026-10-25-team-c-minutes-worker-handoff.md` | 历史保留 | 全文阅读，标明历史配置与验证范围；历史验证未重跑 |
| `docs/plans/2026-10-25-team-c-next-phase-goal-and-acceptance.md` | 历史保留 | 分段全文阅读，明确历史接管依据与当前入口；历史验证未重跑 |
| `docs/plans/2026-10-25-team-c-orphan-probes-gated-handoff.md` | 历史保留 | 全文阅读，标明各轮历史排查与现行检查入口；历史验证未重跑 |
| `docs/plans/2026-10-25-team-c-project-rename-cas-handoff.md` | 历史保留 | 全文阅读，标明历史状态与当前入口；历史验证未重跑 |
| `docs/plans/2026-10-25-team-c-real-runtime-browser-handoff.md` | 历史保留 | 全文阅读，标明历史验证与当前入口；历史验证未重跑 |
| `docs/plans/2026-10-25-team-c-real-runtime-model-browser-handoff.md` | 历史保留 | 全文阅读，标明历史环境与当前入口；历史验证未重跑 |
| `docs/plans/2026-10-25-team-c-realtime-consent-browser-handoff.md` | 历史保留 | 已全文阅读，标明历史交接；未重跑历史验证。 |
| `docs/plans/2026-10-25-team-c-realtime-evidence-promotion-handoff.md` | 历史保留 | 已全文阅读，标明历史交接；未重跑历史验证。 |
| `docs/plans/2026-10-25-team-c-render-unit-create-consumer-handoff.md` | 历史保留 | 已全文阅读，标明历史交接；未重跑历史验证。 |
| `docs/plans/2026-10-25-team-c-saved-asset-read-back-handoff.md` | 历史保留 | 全文阅读，为当时交接及证据记录；历史验证未重跑 |
| `docs/plans/2026-10-25-team-c-teleprompter-state-consumer-handoff.md` | 历史保留 | 全文阅读，为当时交接及证据记录；历史验证未重跑 |
| `docs/plans/2026-10-25-team-c-voice-revision-consumer-handoff.md` | 历史保留 | 全文阅读，为当时交接及证据记录；历史验证未重跑 |
| `docs/plans/2026-10-25-web-accessibility-gate.md` | 历史保留 | 全文阅读，为当时门禁实施记录；历史验证未重跑 |
| `docs/plans/2026-10-26-a02-audio-receipt-handoff.md` | 历史保留 | 全文阅读，为当时交接及证据记录；历史验证未重跑 |
| `docs/plans/2026-10-26-a02-media-kernel-handoff.md` | 历史保留 | 全文阅读，为当时交接及证据记录；历史验证未重跑 |
| `docs/plans/2026-10-26-ci-resource-partitions.md` | 历史保留 | 全文阅读，为当时实施与验证记录；历史验证未重跑 |
| `docs/plans/2026-10-26-paperclip-bootstrap-plan.md` | 历史保留 | 全文阅读，标明历史状态和现行入口；历史环境未复验 |
| `docs/plans/2026-10-26-project-manager-baseline.md` | 历史保留 | 全文阅读，修正旧总控被称为当前入口；历史证据未复验 |
| `docs/plans/2026-10-26-team-a-final-handoff.md` | 历史保留 | 全文阅读，修正旧基线被称为当前状态；历史验证未重跑 |
| `docs/plans/2026-10-26-team-a-review-and-next-phase.md` | 历史保留 | 全文阅读，已有收尾说明与 superseded 状态；历史验证未重跑 |
| `docs/plans/2026-10-26-team-c-final-handoff.md` | 历史保留 | 全文阅读，修正旧基线被称为当前入口；历史验证未重跑 |
| `docs/plans/2026-10-26-team-c-next-phase-execution-and-collaboration.md` | 历史保留 | 全文阅读，已有收尾说明与 superseded 状态；历史验证未重跑 |
| `docs/plans/2026-10-27-delegra-legion-closed-loop.md` | 历史保留 | 全文阅读，标明历史配置、授权和现行入口；平台状态未复验 |
| `docs/plans/2026-10-30-zero-dispatch-settlement.md` | 历史保留 | 全文阅读，标明固定基线和候选方案的时点边界；历史验证及部署未复验 |
| `docs/plans/README.md` | 已核对 | 核对当前 PRD、需求矩阵、结构化来源和只读刷新入口及 77 个本地链接；带日期条目作为历史索引，原时点摘要未逐项重验 |
| `docs/plans/binding-disable-cancellation.md` | 已核对 | 全文对照当前需求、准入屏障、目标账本与受限权限、网关取消恢复、租约事务契约和隔离测试设计；补充既有 submit 的未外发失败结算条件，未运行数据库、真实引擎或多机验收 |
| `docs/plans/delivery-tracking.md` | 部分核对 | 核对生成入口、结构化来源和快照标记；逐项事实与证据仍待核对 |
| `docs/po-sm-operating-playbook.md` | 已核对 | 全文对照交付 SOP 和 fleet-status 查询、分类及输出；修正门禁、等待、跨公司计数和模式差异，未连接平台数据库验证运行状态 |
| `docs/prd.md` | 部分核对 | 核对 65 个需求 ID、锚点和 Epic 与结构化清单一致；对照部署单元澄清持久内容与实时流的跨进程传输及代理独立上游缺口，对照 CPU VAD 与辅助能力设计限定输入规格并合并重复验收项，对照保留策略限定普通生成音频 30 天目标；对照节点控制器区分预热目标期限与当前分段超时，对照资源治理限定 full_segment 预算条件，补全实时 ASR 首预览计时起点，修正 TTS 默认 pipeline 指令上限归属，补全工作台恢复的执行入口屏障及当前缺口；区分有声书生成 v1 契约和 v2 实际接线，去除对历史 GPU 主机服务的固定假设，核对 binding 管理操作、能力目录字段类型与边界及 profile 范围和当前发布缺口；其余验收语义待核对 |
| `docs/project-manager-guide.md` | 已核对 | 全文对照根规约、交付 SOP、军团职责及矩阵刷新入口；15 个本地链接目标存在，未核验平台实际部署与权限 |
| `docs/speechnexus-product-architecture.md` | 部分核对 | 局部对照代码、契约或历史证据；澄清部署单元可共置或分机及 Host / Origin 中间件的路径范围，对照实时、音色与工作台入口、文件 ASR 分段汇总、共享密钥、导出组合根、默认值快照、预设生命周期、绑定停用 / 数据删除、Job 派发及故障矩阵；其余内容待核对 |
| `docs/validation/README.md` | 已核对 | 全文核对四个索引链接目标；不代表各目标正文已审阅 |
| `docs/validation/ci-validation.md` | 部分核对 | 对照路由、两进程覆盖率及六分片分配与证据校验、镜像及最终门禁、制品下载、浏览器预取、Design Token、无障碍规则、栅格对比度和真实模型资格入口；澄清差异检查依赖准确基线、文档路径仍执行镜像范围门禁和 PG 探针前置条件，修正分片需求收集脚本路径；2026-11-04 只读核对 main 的三个必需检查及 Actions app 绑定一致；其余脚本细节待核对，历史性能按原时点保留 |
| `docs/validation/coordination-request-change-checks.md` | 已核对 | 全文对照 Make/CI、检查器及共享比较和引用解析内核、请求/结果正反例与 Python/JS fixture；不代表事务语义或部署已验收 |
| `docs/validation/http-contract-change-checks.md` | 已核对 | 全文对照 CI、脚本、比较与引用解析内核及正反例；澄清源校验、JSON 报告与 Python 解析器的覆盖边界，不代表完整协议兼容 |
| `docs/validation/realtime-contract-change-checks.md` | 已核对 | 全文对照 CI、Make 目标、检查脚本、绑定配置及正反例测试；结构检查不证明实时行为或生产互通 |
| `docs/plans/document-review-tracking.md` | 待核对 | 清单自身随最终盘点核验 |

## 其他受版本控制的文档制品

包含证据数据、候选契约、图和图生成资源；按用途核对，不将历史数据改写为当前事实。

| 文档（相对仓库根目录） | 状态 | 核对依据与结果 |
| --- | --- | --- |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/01-cluster-overview-4k.check.json` | 已核对 | 对照综合报告和脚本核对检查项目、状态及 SVG / PNG 摘要；是历史渲染记录，未重跑布局检查 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/01-cluster-overview-4k.content.json` | 已核对 | 核对 JSON 语法、构图脚本的生成入口和渲染脚本的文字清单用途；清单只检查选定文字，不证明拓扑语义 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/01-cluster-overview-4k.png` | 已核对 | 核对尺寸、历史摘要及预览缩放关系；图面按目标架构审阅，数量是示例，不证明实际部署 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/01-cluster-overview-4k.preview.png` | 已核对 | 查看完整图面，对照图示说明、构图源及 ADR 0004 核对角色、节点、存储与预算；不证明实际部署或资格 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/01-cluster-overview-4k.svg` | 已核对 | 核对构图源、历史摘要与渲染图面；为目标拓扑示意，不证明实际装配 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/02-model-instance-topology-4k.check.json` | 已核对 | 对照综合报告和脚本核对检查项目、状态及 SVG / PNG 摘要；是历史渲染记录，未重跑布局检查 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/02-model-instance-topology-4k.content.json` | 已核对 | 核对 JSON 语法、构图脚本的生成入口和渲染脚本的文字清单用途；清单只检查选定文字，不证明拓扑语义 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/02-model-instance-topology-4k.png` | 已核对 | 核对尺寸、历史摘要及预览缩放关系；图面按目标架构审阅，不证明模型部署或资格 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/02-model-instance-topology-4k.preview.png` | 已核对 | 查看完整图面，对照构图源、ADR 0002 / 0004 与实例契约核对模型、运行时、实例、资源池和依赖；数量与容量为示例 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/02-model-instance-topology-4k.svg` | 已核对 | 核对构图源、历史摘要与渲染图面；为目标拓扑示意，不证明实际装配 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/assets/application.png` | 已核对 | 核对构图脚本用途，素材字节与总览 SVG 内嵌图一致；仅作装饰，不表达部署事实 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/assets/compute.png` | 已核对 | 核对构图脚本用途，素材字节与总览 SVG 内嵌图一致；仅作装饰，不表达部署事实 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/assets/data.png` | 已核对 | 核对构图脚本用途，素材字节与总览 SVG 内嵌图一致；仅作装饰，不表达部署事实 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/build_diagrams.py` | 已核对 | 全文核对模型、布局、连接和输出逻辑，对照图面、模型清单及目标架构；未重新生成图文件 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/image-prompts.json` | 已核对 | 核对 JSON 及三个素材键与构图用途；提示词是历史制作资料，不代表当前部署事实 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/model-catalog.json` | 已核对 | 8 项的全部字段与构图脚本生成源一致，模型、引擎及加载方向与 ADR 0002 对照一致；这是目标选型，实际制品和部署资格未验证 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/render_and_validate.cjs` | 已核对 | 全文核对渲染、标记元素布局、素材与模型目录断言及报告输出；语法检查通过，未重跑浏览器渲染 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/validation.json` | 已核对 | 2026-11-03 报告的三份素材及两组 SVG / PNG 摘要匹配当前文件，与单图报告一致；对照脚本核对检查范围，未重跑渲染 |
| `docs/audits/2026-10-25-team-c-acceptance-map.json` | 历史保留 | 对照原 AC 说明核对 41 项、278 子条件及 not_run 状态；未复核原始证据 |
| `docs/audits/2026-10-26-team-a-closeout-evidence.json` | 历史保留 | 全文阅读并对照最终交接的版本、计数与资格边界；原始日志和制品未复验 |
| `docs/audits/2026-10-26-team-a-issue-handoff.json` | 历史保留 | 核对快照时间、来源、交接引用及 40 个唯一编号；未重查历史远端状态 |
| `docs/audits/2026-10-26-team-c-operation-coverage.json` | 历史保留 | 对照固定 head 的报告核对 52 项及 42/10 状态；不代表当前操作覆盖 |
| `docs/audits/evidence/2026-11-03-tts-prepare.json` | 历史保留 | 首次窗口的结构化证据；核对报告引用及候选版本，原始记录未逐项重验 |
| `docs/audits/evidence/2026-11-03-tts-window-2.json` | 历史保留 | 第二窗口的结构化证据；核对报告引用、版本及 SDK 闭环未通过标记，原始记录未逐项重验 |
| `docs/audits/evidence/2026-11-03-tts-window-3.json` | 历史保留 | 第三窗口的结构化证据；核对报告引用、版本及 SDK 503 标记，原始记录未逐项重验 |
| `docs/audits/evidence/2026-11-03-tts-window-4.json` | 历史保留 | 第四窗口的结构化证据；核对报告引用、版本及 SDK 503 标记，原始记录未逐项重验 |
| `docs/audits/evidence/2026-11-03-tts-window-5.json` | 历史保留 | 第五窗口的结构化证据；核对 SDK 成功但关联未证明的边界，原始记录未逐项重验 |
| `docs/audits/evidence/2026-11-03-tts-window-6.json` | 历史保留 | 第六窗口的结构化证据；核对报告引用及固定版本，原始记录未逐项重验 |
| `docs/operations/release-evidence-verification-output.txt` | 历史保留 | 2026-10-28 固定版本的合成数据验证输出；不代表当前发布资格，原始运行未重做 |
| `docs/plans/2026-10-23-implementation-issues.json` | 历史保留 | 核对原基线、索引引用及 38 个唯一编号；旧编排不代表当前状态 |
| `docs/plans/2026-10-24-team-c-c02-download-contract-candidate.yaml` | 历史保留 | 全文阅读，核对计划、交接及候选测试引用；该候选路径未出现在当前工作台 canonical 契约中 |
| `docs/plans/2026-10-24-team-c-gateway-clock-candidate.patch` | 历史保留 | 核对两行候选内容及原计划、交接引用；仅为固定历史候选，未应用或重跑验证 |
| `docs/plans/delivery-issues.json` | 部分核对 | JSON 语法、重复键及非有限数值检查通过；来源与语义待核对 |
