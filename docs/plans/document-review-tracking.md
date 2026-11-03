# 文档逐篇审阅清单

本清单用于跟踪文档用途判定，以及对仍作为当前设计、操作或验证依据的内容所做的核对。既往修改或链接检查本身不代表整篇已审完。历史快照保留原时点事实，无需逐条与现状对齐；仅在其仍承担当前指导、存在误导性入口或需要纠正已知事实错误时处理相关部分。不能仅凭目录名或日期断定文档没有持续用途。

完成一篇后记录用途判定、核对依据和结果；部分核对注明剩余范围。确认仅为历史记录且无需调整的条目标为“历史保留”，不表示其原始证据已逐项复核。尚未判定用途的条目继续标为待核对；新增或迁移文件时同步调整清单。

| 文档（相对仓库根目录） | 状态 | 核对依据与结果 |
| --- | --- | --- |
| `docs/README.md` | 部分核对 | 全文阅读，核对本地链接、CI 数据库版本、上线前检查、Job 派发、同步转写及运行投递状态；其他条目的职责与证据待核对 |
| `docs/agent-guide.md` | 已核对 | 全文对照根规则、五个项目技能与交付 SOP；49 个链接的本地目标均存在，外部参考保留标注的历史核验时间 |
| `docs/agent-verification.md` | 已核对 | 对照 Make 目标、验证脚本、工具链声明、CI 指南和 Workbench PG 探针清理路径；核对服务前置、副作用与 verify 覆盖限制，未启动服务或执行集成测试 |
| `docs/architecture/README.md` | 已核对 | 逐项核对 23 个本地链接及职责描述；不代表所链接正文已审 |
| `docs/architecture/contract-ownership.md` | 已核对 | 逐项对照机器矩阵的 18 端口写入范围和 7 类查询、私有表及授权来源；更新消费者、运行控制装配与用量消费状态，未重跑集成测试 |
| `docs/architecture/coordination-transactions.md` | 部分核对 | 局部对照代码、契约或历史证据；其余内容待核对 |
| `docs/architecture/data-deletion.md` | 部分核对 | 全文阅读并核对删除契约、计划期限、工作台依赖分类与活动生成阻断、schema 版本、启动重放前提及清理装配；跨模块竞态待核对，未执行删除或恢复演练 |
| `docs/architecture/deployment-units.md` | 部分核对 | 核对 Compose 服务、Web 代理、镜像及依赖、主要进程入口、Job 派发失败关闭与媒体检查装配，修正 profile 与历史证据，补充 binding 停用及同步 TTS 结算 worker；其余单元、拓扑和资格未逐项核实 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/README.md` | 部分核对 | 对照构图脚本、ADR、目录和图像摘要；未重渲染或核验全部拓扑 |
| `docs/architecture/distributed-coordination-schema.md` | 部分核对 | 已全文阅读并核对关键边界，其他细节仍待逐项核对 |
| `docs/architecture/execution-command.md` | 部分核对 | 核对提交摘要、重放、文本流及文件 ASR/TTS 消费入口；区分 codec 对象正文设计要求与命令 schema 覆盖，其他命令类型与 adapter 边界待核对 |
| `docs/architecture/openai-compatibility.md` | 部分核对 | 核对 SDK 版本、实时装配与用量、REST 字段与幂等拒绝、响应格式、公共错误字段、SDK 测试样例、版本字段、实时更新结构及 SpeechRail 固定来源；记录转写未知字段的契约与实现错误码差异，其他扩展语义待核对 |
| `docs/architecture/runtime-governance.md` | 部分核对 | 局部对照代码、契约或历史证据；其余内容待核对 |
| `docs/architecture/speech-api.md` | 部分核对 | 局部对照代码、契约或历史证据；其余内容待核对 |
| `docs/architecture/speechrail-reuse.md` | 部分核对 | 核对项目复用政策、固定 commit、10 个路径与许可证；上游行为及消费者待核对 |
| `docs/architecture/technical-blueprint.md` | 部分核对 | 局部对照代码、契约或历史证据；其余内容待核对 |
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
| `docs/audits/2026-11-03-controlled-tts-prepare.md` | 部分核对 | 全文阅读并对照第六窗口证据文件及摘要，修正最新结论；各窗口原始事实尚未逐项核验 |
| `docs/audits/README.md` | 部分核对 | 全文阅读并修正历史交接入口、补充 TTS 审计入口；各历史摘要与原文待逐项核对 |
| `docs/components/README.md` | 已核对 | 核对索引中各目标文件、标题与候选状态；不代表目标正文已审 |
| `docs/components/audio-asset-inspection.md` | 历史保留 | 2026-10-26 候选与资格记录，保留原时点事实；当前入口的 TextAssetInspector 装配已对照源码，历史证据未重验 |
| `docs/components/binding-defaults-producer.md` | 部分核对 | 核对配置、Compose、契约入口、创建及固定读取处理、错误响应、声明值域校验与角色生命周期；澄清发布状态读取前提及回环客户端范围，未重跑集成测试 |
| `docs/components/binding-parameter-defaults.md` | 部分核对 | 对照快照 schema、共享字段、解析器和生产者校验核对声明及持久化范围；当前检索未见最终合并消费入口，其他消费者与测试细节仍待核对 |
| `docs/components/controlled-asr-generation.md` | 部分核对 | 全文阅读，核对凭据派生、模板约束、私有描述及成功终态停止证据；更新真实实例和整代次结算的历史证据，其他停止与运行资格待核对 |
| `docs/components/cpu-vad.md` | 已核对 | 对照策略、缓冲、CPU 适配器、固定模型声明、文件消费者、Compose 静态配置及测试入口；未运行模型或验证实际部署资格 |
| `docs/components/execution-gateway.md` | 部分核对 | 核对 HTTP/装配、提交及屏障、停止、恢复、正文清理、观察、输出区间和具名结果；核验测试路径并补齐 MinIO 前置，测试覆盖细节仍待核对 |
| `docs/components/execution-staging-results.md` | 已核对 | 对照回执契约、迁移、列授权、登记 CAS/重放、覆盖检查、HTTP 查询及测试入口；未运行集成测试 |
| `docs/components/file-asr-audio.md` | 已核对 | 对照共享媒体模块、源格式、限制、时钟投影、测试入口及已知消费者装配；未重跑测试或验证部署 |
| `docs/components/file-asr-recognition.md` | 已核对 | 逐节静态对照参数、协议、装配、执行与测试清单；未重新实测 |
| `docs/components/file-asr-results.md` | 已核对 | 逐节静态对照结果聚合、私有对象、命令与相关测试；未运行服务 |
| `docs/components/file-tts.md` | 已核对 | 全文静态对照策略、HTTP、命令、编码器、worker 与历史探针记录；未重跑测试或历史探针 |
| `docs/components/frozen-execution-inputs.md` | 已核对 | 对照命令桥失败分类、对象版本与传输期限、ASR 参数映射及集成测试入口；未运行集成测试或验证部署 |
| `docs/components/gateway-execution-client.md` | 已核对 | 对照客户端配置、认证、结果查询、停止身份、重试、调用方装配及测试入口；未运行测试或验证目标部署 |
| `docs/components/model-binding-drafts.md` | 部分核对 | 对照草稿和目录读取授权、生命周期事务、0011 降级迁移、相关测试定义及 HTTP 源和结构检查；修正锁、期限、事件原因、禁用意图权限及检查范围，未运行集成测试；其他消费者细节待核对 |
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
| `docs/components/repository-layout.md` | 部分核对 | 已对照工作区、迁移目录、接口目录、共享 Web 包、Compose、进程配置与验证入口修正布局及配置范围；其余内容待核对 |
| `docs/components/runtime-controller-client.md` | 已核对 | 逐节静态对照客户端、节点观察循环、持久投递和测试清单；未运行服务 |
| `docs/components/synchronous-speech.md` | 已核对 | 逐节静态对照 TTS 装配、准入锁序、恢复、结算预检、配置和错误处理；未运行服务 |
| `docs/components/transcript-import-and-text-export.md` | 已核对 | 逐节静态对照来源校验、导入事务、编辑器、下载和公开契约；未运行服务 |
| `docs/components/trusted-node-stop.md` | 部分核对 | 局部对照代码、契约或历史证据；其余内容待核对 |
| `docs/components/workbench-content-downloads.md` | 已核对 | 逐节静态对照下载事务、HTTP、对象存储、浏览器消费者及公开契约；未运行服务 |
| `docs/components/workbench-export-integrity.md` | 已核对 | 逐节静态对照导出 worker、媒体校验、配置、组合根、持久写入及测试清单；未运行服务 |
| `docs/components/workbench-voice-catalogue.md` | 已核对 | 逐节静态对照公开契约、适配器、运行配置及 Web 消费者；未运行服务 |
| `docs/components/zero-dispatch-contract-core.md` | 部分核对 | 全文阅读并对照候选 schema、六个生成入口与索引；尚未逐项核对契约约束和历史接收证据 |
| `docs/decisions/0001-product-boundaries-and-authentication-scope.md` | 部分核对 | 全文阅读并对照 PRD 产品与认证边界；其他实现细节待核对 |
| `docs/decisions/0002-speech-models-engines-and-loading.md` | 部分核对 | 全文阅读，核对决策索引与资格边界；模型选择和运行资格需逐项对照当前证据 |
| `docs/decisions/0003-workbench-llm-network-boundary.md` | 部分核对 | 全文阅读并抽核 PRD、配置和 HTTP 投影；未实测部署出站限制 |
| `docs/decisions/0004-cluster-ready-architecture-and-single-node-mvp.md` | 部分核对 | 全文阅读并核对历史表名及当前协调 schema；其他细节待核对 |
| `docs/decisions/0005-complete-execution-and-data-lifecycle-contracts.md` | 部分核对 | 全文阅读并核对历史边界与后续引用；其余事实及消费者待逐项核对 |
| `docs/decisions/0006-cluster-coordination-and-storage-portability.md` | 部分核对 | 全文阅读并核对历史边界与后续引用；其余事实及消费者待逐项核对 |
| `docs/decisions/0007-legacy-coordination-retirement.md` | 部分核对 | 全文阅读并核对历史边界与后续引用；其余事实及消费者待逐项核对 |
| `docs/decisions/0008-runtime-intents-before-controller-observation.md` | 部分核对 | 全文阅读并核对历史边界与后续引用；其余事实及消费者待逐项核对 |
| `docs/decisions/0009-workbench-explicit-binding-http-version.md` | 部分核对 | 全文阅读并核对历史边界与后续引用；其余事实及消费者待逐项核对 |
| `docs/decisions/0010-private-audio-receipt-compatibility.md` | 部分核对 | 全文阅读并核对历史边界与后续引用；其余事实及消费者待逐项核对 |
| `docs/decisions/0011-distributed-deployment-and-platform-boundaries.md` | 部分核对 | 全文阅读并对照根工作约定与部署文档；平台运行未独立验证 |
| `docs/decisions/0012-public-job-surface.md` | 部分核对 | 全文阅读并核对公开提交与完整准入原子边界；其他决策语义待核对 |
| `docs/decisions/0013-binding-lifecycle-admin-http.md` | 部分核对 | 全文阅读并抽核相关实现或历史证据；其他细节待核对 |
| `docs/decisions/README.md` | 部分核对 | 全文阅读并抽核相关实现或历史证据；其他细节待核对 |
| `docs/design/ui-ux-design-framework-and-tokens.md` | 部分核对 | 全文阅读，对照 CI、Token 来源、文案、状态和随机调用扫描范围与品牌组件，修正相关描述；其他细节待核对 |
| `docs/development-environment.md` | 部分核对 | 对照初始化、服务、浏览器冒烟和契约检查脚本，修正安装、权限、端口及检查边界；参考环境版本和历史运行状态未实测 |
| `docs/operations/README.md` | 已核对 | 核对目录定位、索引说明及所有本地链接目标；历史快照已标识 |
| `docs/operations/agent-delivery-sop.md` | 部分核对 | 全文阅读并对照根规约的授权与工作区收尾、验证指南入口；平台实际能力及需求关联待核对 |
| `docs/operations/backfill-binding-publication-evidence.md` | 已核对 | 对照 CLI、映射约束与去重计数、回填事务、授权声明及 0021 回退锁；未连接生产库或运行回填 |
| `docs/operations/binding-activation-verification.md` | 部分核对 | 全文阅读并抽查 PG 并发和清理测试；未运行 PG 或核对全部矩阵 |
| `docs/operations/binding-disable-worker.md` | 已核对 | 对照配置范围、角色授权列、URL 策略、处理循环、异常分支及原租约结算入口；未连接部署或执行停用 |
| `docs/operations/binding-lifecycle.md` | 部分核对 | 全文阅读并核对授权、停用响应、主要操作与部分冲突分支；补充 HTTP 匿名审计身份，并发与恢复事务待核对 |
| `docs/operations/builtin-voice-qualification.md` | 部分核对 | 核对受控窗口、资格入口、制品脚本摘要、主要授权及命令退出分支；probe 内部验证细节待核对 |
| `docs/operations/capacity-manifest.md` | 部分核对 | 核对聚合检查、占位值、校验器退出码及发布前后步骤；澄清 fixture、live 查询隔离与缺少工作区配额的范围，其余内容待核对 |
| `docs/operations/compose-minimal.md` | 部分核对 | 对照 Compose 服务、迁移链、配置准备、基础角色授权、停用 worker 扫描与计数，修正权限及失败边界并更新章节引用；未执行部署，其他恢复细节待核对 |
| `docs/operations/file-asr-binding-handoff.md` | 部分核对 | 核对候选选型、绑定定义的支持域和默认值、离线制品与忽略元数据校验、工具退出和输出、网关输出格式及停止证据边界；其余内容待核对 |
| `docs/operations/file-asr-runtime.md` | 部分核对 | 对照配置、对象版本、worker 取消及结果登记恢复；停止及集成测试证据待核对 |
| `docs/operations/file-tts-binding-handoff.md` | 部分核对 | 核对绑定定义的候选参数和默认值、指令长度契约上限、离线工具的忽略元数据与输出边界、后续 SDK 与租约结算证据；其余内容待核对 |
| `docs/operations/file-tts-runtime.md` | 部分核对 | 核对双 lane 共享 worker、配置及单请求音频上限、后续 SDK 和租约结算证据；其余内容待核对 |
| `docs/operations/local-end-to-end.md` | 部分核对 | 核对历史边界与角色脚本并纠正数量；剩余历史输出和证据待逐项复核 |
| `docs/operations/paperclip/environment-readiness.md` | 历史保留 | 已明确标注为旧环境与部署快照，当前流程指向交付 SOP；原始证据未逐项复核 |
| `docs/operations/paperclip/roles/common.md` | 部分核对 | 全文对照工作流与交付 SOP；实际部署未核实 |
| `docs/operations/paperclip/roles/evidence.md` | 部分核对 | 全文对照岗位与证据边界；部署配置及实际原件未核实 |
| `docs/operations/paperclip/roles/execution.md` | 部分核对 | 全文对照工作约定和交付 SOP；实际部署配置与运行权限未核实 |
| `docs/operations/paperclip/roles/manager.md` | 部分核对 | 全文对照项目约定及交付 SOP；实际部署和岗位权限未核实 |
| `docs/operations/paperclip/roles/platform.md` | 部分核对 | 全文对照工作约定和交付 SOP；实际部署配置与运行权限未核实 |
| `docs/operations/paperclip/roles/qa.md` | 部分核对 | 全文对照项目约定及交付 SOP；实际部署和岗位权限未核实 |
| `docs/operations/paperclip/roles/reviewer.md` | 部分核对 | 全文对照工作约定和交付 SOP；实际部署配置与运行权限未核实 |
| `docs/operations/paperclip/roles/workbench.md` | 部分核对 | 全文对照项目约定及交付 SOP；实际部署和岗位权限未核实 |
| `docs/operations/paperclip/runtime-incidents.md` | 部分核对 | 全文阅读并修正旧保全脚本及删除检查的适用边界；历史事故原件未核验 |
| `docs/operations/paperclip.md` | 部分核对 | 核对现行工作流入口及历史环境边界；岗位、配置与平台映射尚未逐项核实 |
| `docs/operations/production-coordination-storage.md` | 部分核对 | 核对 0002 的 13 张新增表和 4 个查询索引、迁移 head、运行角色列级授权及释放守卫视图投影；跨表约束与其他生产事务细节待核对 |
| `docs/operations/production-runtime-delivery.md` | 已核对 | 对照配置、租约、unknown 路径、迁移、授权、ACK、Compose 及测试入口；未执行集成测试或验证目标部署 |
| `docs/operations/publish-binding-revision.md` | 部分核对 | 核对 CLI 观察、退出分类及 publication 与草稿检查顺序、发布意图重放和已有事实分支的 CAS 边界；补充连接池未捕获异常的退出码限制，其他事务分支及数据库实测待核对 |
| `docs/operations/qualification-report.md` | 部分核对 | 核对示例命令、场景状态、有效期、notes 及 limitations 校验边界，明确通用发布命令不读取报告，并区分快照 fixture、离线脚本退出码和产品组件的覆盖范围；其余内容待核对 |
| `docs/operations/reconciliation-recovery.md` | 部分核对 | 核对 abort 恢复查询、unknown 停止证据规则及查询 2、3 所涉主要表字段与证据标志类型；修正资源释放与预算归还顺序及代码锚点描述；查询执行及其余内容待核对 |
| `docs/operations/register-engine.md` | 部分核对 | 核对健康探测及重定向证据、host/owner/quota 校验、同代重放与 readiness 刷新、参数范围、配方覆盖规则及验证查询；补充提交结果未知、查询证据边界和操作员确认的投递阻断，其余内容待核对 |
| `docs/operations/release-preflight.md` | 已核对 | 对照脚本检查入口、配置加载和 C0–C12 判断；修正版本、身份、临时文件、资格场景、Compose 文本匹配及固定快照边界，未执行会访问服务或写入的检查 |
| `docs/operations/release-readiness.md` | 历史保留 | 全文阅读，固定候选与核验时点的发布就绪快照；运维索引标为历史，未逐项复核历史证据 |
| `docs/operations/synchronous-transcription.md` | 部分核对 | 核对配置、配方约束、请求校验顺序、媒体探测证据范围、提交与轮询路径、HTTP 请求和媒体错误映射及网关主要错误映射；其余实现细节待核对 |
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
| `docs/plans/README.md` | 部分核对 | 核对历史索引定位及 77 个本地链接目标；历史摘要尚未逐项核实 |
| `docs/plans/binding-disable-cancellation.md` | 部分核对 | 全文阅读，对照 public Job 锁及 Compose 入口修正自相矛盾的旧描述；其余协调和验收细节待核对 |
| `docs/plans/delivery-tracking.md` | 部分核对 | 核对生成入口、结构化来源和快照标记；逐项事实与证据仍待核对 |
| `docs/po-sm-operating-playbook.md` | 部分核对 | 核对 fleet-status 参数及退出码；其他章节与数据库行为待核对 |
| `docs/prd.md` | 部分核对 | 核对 65 个需求 ID、锚点和 Epic 与结构化清单一致；逐项验收语义待核对 |
| `docs/project-manager-guide.md` | 部分核对 | 全文阅读，对照根规约、交付入口和矩阵刷新方式；平台运行与其余引用细节待核对 |
| `docs/speechnexus-product-architecture.md` | 部分核对 | 局部对照代码、契约或历史证据；其余内容待核对 |
| `docs/validation/README.md` | 已核对 | 全文核对四个索引链接目标；不代表各目标正文已审阅 |
| `docs/validation/ci-validation.md` | 部分核对 | 对照当前路由白名单、两进程覆盖率入口及六分片工作流；子脚本细节待核对，历史性能按原时点保留 |
| `docs/validation/coordination-request-change-checks.md` | 部分核对 | 全文阅读并核对 Make/CI 入口、请求/结果方向及正反例清单；检查器比较内核的全部限制待核对 |
| `docs/validation/http-contract-change-checks.md` | 部分核对 | 核对 CI、脚本与命令；HEAD 离线 HTTP 和 speech 结构检查无发现，正反例待核对 |
| `docs/validation/realtime-contract-change-checks.md` | 部分核对 | 核对 CI、脚本与命令；HEAD 离线实时结构检查无发现，正反例待核对 |
| `docs/plans/document-review-tracking.md` | 待核对 | 清单自身随最终盘点核验 |

## 其他受版本控制的文档制品

包含证据数据、候选契约、图和图生成资源；按用途核对，不将历史数据改写为当前事实。

| 文档（相对仓库根目录） | 状态 | 核对依据与结果 |
| --- | --- | --- |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/01-cluster-overview-4k.check.json` | 部分核对 | 历史报告的 SVG 和 PNG 摘要匹配当前文件；未重跑布局检查 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/01-cluster-overview-4k.content.json` | 部分核对 | JSON 语法、重复键及非有限数值检查通过；来源与语义待核对 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/01-cluster-overview-4k.png` | 部分核对 | 确认 4K 尺寸及部署文档引用；图面内容待核对 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/01-cluster-overview-4k.preview.png` | 部分核对 | 确认尺寸、引用及图面标注为目标架构；拓扑细节待逐项核对 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/01-cluster-overview-4k.svg` | 部分核对 | 确认 SVG 格式及部署文档引用；图面内容待核对 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/02-model-instance-topology-4k.check.json` | 部分核对 | 历史报告的 SVG 和 PNG 摘要匹配当前文件；未重跑布局检查 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/02-model-instance-topology-4k.content.json` | 部分核对 | JSON 语法、重复键及非有限数值检查通过；来源与语义待核对 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/02-model-instance-topology-4k.png` | 部分核对 | 确认 4K 尺寸及部署文档引用；图面内容待核对 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/02-model-instance-topology-4k.preview.png` | 部分核对 | 确认尺寸、引用及示例容量边界；模型与拓扑细节待逐项核对 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/02-model-instance-topology-4k.svg` | 部分核对 | 确认 SVG 格式及部署文档引用；图面内容待核对 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/assets/application.png` | 部分核对 | 确认 PNG 格式及构图说明；素材与图面关系待核对 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/assets/compute.png` | 部分核对 | 确认 PNG 格式及构图说明；素材与图面关系待核对 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/assets/data.png` | 部分核对 | 确认 PNG 格式及构图说明；素材与图面关系待核对 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/build_diagrams.py` | 部分核对 | 核对 SVG、内容清单及模型清单输出入口；未重新生成或逐项审查绘图逻辑 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/image-prompts.json` | 部分核对 | JSON 语法、重复键及非有限数值检查通过；来源与语义待核对 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/model-catalog.json` | 部分核对 | 模型、引擎及加载方向与 ADR 0002 对照一致；实际制品和部署资格未验证 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/render_and_validate.cjs` | 部分核对 | 核对浏览器渲染、布局及素材检查入口；未重跑或逐项审查所有断言 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/validation.json` | 部分核对 | 2026-10-26 报告中的三份素材摘要匹配当前文件；未重跑渲染或验证全部结论 |
| `docs/audits/2026-10-25-team-c-acceptance-map.json` | 历史保留 | 对照原 AC 说明核对 41 项、278 子条件及 not_run 状态；未复核原始证据 |
| `docs/audits/2026-10-26-team-a-closeout-evidence.json` | 历史保留 | 全文阅读并对照最终交接的版本、计数与资格边界；原始日志和制品未复验 |
| `docs/audits/2026-10-26-team-a-issue-handoff.json` | 历史保留 | 核对快照时间、来源、交接引用及 40 个唯一编号；未重查历史远端状态 |
| `docs/audits/2026-10-26-team-c-operation-coverage.json` | 历史保留 | 对照固定 head 的报告核对 52 项及 42/10 状态；不代表当前操作覆盖 |
| `docs/audits/evidence/2026-11-03-tts-prepare.json` | 部分核对 | 核对审计报告引用及候选版本；原始记录和验证语义待复核 |
| `docs/audits/evidence/2026-11-03-tts-window-2.json` | 部分核对 | 核对审计报告引用、版本及 SDK 闭环未通过标记；原始记录待复核 |
| `docs/audits/evidence/2026-11-03-tts-window-3.json` | 部分核对 | 核对审计报告引用、版本及 SDK 503 失败标记；原始记录待复核 |
| `docs/audits/evidence/2026-11-03-tts-window-4.json` | 部分核对 | 核对审计报告引用、版本及 SDK 503 失败标记；原始记录待复核 |
| `docs/audits/evidence/2026-11-03-tts-window-5.json` | 部分核对 | 核对报告引用及 SDK 成功但关联未证明的边界；原始记录待复核 |
| `docs/audits/evidence/2026-11-03-tts-window-6.json` | 部分核对 | 核对审计报告引用及固定版本；原始记录和验证语义待复核 |
| `docs/operations/release-evidence-verification-output.txt` | 历史保留 | 2026-10-28 固定版本的合成数据验证输出；不代表当前发布资格，原始运行未重做 |
| `docs/plans/2026-10-23-implementation-issues.json` | 历史保留 | 核对原基线、索引引用及 38 个唯一编号；旧编排不代表当前状态 |
| `docs/plans/2026-10-24-team-c-c02-download-contract-candidate.yaml` | 历史保留 | 全文阅读，核对计划、交接及候选测试引用；该候选路径未出现在当前工作台 canonical 契约中 |
| `docs/plans/2026-10-24-team-c-gateway-clock-candidate.patch` | 历史保留 | 核对两行候选内容及原计划、交接引用；仅为固定历史候选，未应用或重跑验证 |
| `docs/plans/delivery-issues.json` | 部分核对 | JSON 语法、重复键及非有限数值检查通过；来源与语义待核对 |
