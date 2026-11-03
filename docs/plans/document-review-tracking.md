# 文档逐篇审阅清单

本清单用于跟踪文档用途判定，以及对仍作为当前设计、操作或验证依据的内容所做的核对。既往修改或链接检查本身不代表整篇已审完。历史快照保留原时点事实，无需逐条与现状对齐；仅在其仍承担当前指导、存在误导性入口或需要纠正已知事实错误时处理相关部分。不能仅凭目录名或日期断定文档没有持续用途。

完成一篇后记录用途判定、核对依据和结果；部分核对注明剩余范围。确认仅为历史记录且无需调整的条目标为“历史保留”，不表示其原始证据已逐项复核。尚未判定用途的条目继续标为待核对；新增或迁移文件时同步调整清单。

| 文档（相对仓库根目录） | 状态 | 核对依据与结果 |
| --- | --- | --- |
| `docs/README.md` | 部分核对 | 全文阅读并抽核相关实现或历史证据；其他细节待核对 |
| `docs/agent-guide.md` | 部分核对 | 全文阅读并抽核相关实现或历史证据；其他细节待核对 |
| `docs/agent-verification.md` | 部分核对 | 对照 Make 目标、验证脚本、工具链声明和 Workbench PG 探针清理路径，补充容器副作用、verify 覆盖限制及日志生成条件；其他检查细节待核对 |
| `docs/architecture/README.md` | 已核对 | 逐项核对 23 个本地链接及职责描述；不代表所链接正文已审 |
| `docs/architecture/contract-ownership.md` | 部分核对 | 已全文阅读并核对关键边界，其他细节仍待逐项核对 |
| `docs/architecture/coordination-transactions.md` | 部分核对 | 局部对照代码、契约或历史证据；其余内容待核对 |
| `docs/architecture/data-deletion.md` | 部分核对 | 全文阅读并核对删除契约、启动重放前提及清理装配；跨模块竞态待核对 |
| `docs/architecture/deployment-units.md` | 部分核对 | 核对 Compose 服务、Web 代理、镜像及依赖，修正 profile 与历史证据；其余单元、拓扑和资格未逐项核实 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/README.md` | 部分核对 | 对照构图脚本、ADR、目录和图像摘要；未重渲染或核验全部拓扑 |
| `docs/architecture/distributed-coordination-schema.md` | 部分核对 | 已全文阅读并核对关键边界，其他细节仍待逐项核对 |
| `docs/architecture/execution-command.md` | 部分核对 | 全文阅读并核对提交摘要、重放和文本流契约；adapter 与消费者待核对 |
| `docs/architecture/openai-compatibility.md` | 部分核对 | 局部对照代码、契约或历史证据；其余内容待核对 |
| `docs/architecture/runtime-governance.md` | 部分核对 | 局部对照代码、契约或历史证据；其余内容待核对 |
| `docs/architecture/speech-api.md` | 部分核对 | 局部对照代码、契约或历史证据；其余内容待核对 |
| `docs/architecture/speechrail-reuse.md` | 部分核对 | 核对项目复用政策、固定 commit、10 个路径与许可证；上游行为及消费者待核对 |
| `docs/architecture/technical-blueprint.md` | 部分核对 | 局部对照代码、契约或历史证据；其余内容待核对 |
| `docs/archive/legacy-postgresql-coordination.md` | 历史保留 | 全文阅读，明确标为旧原型并指向现行架构；无需按当前实现改写，原始证据未逐项复核 |
| `docs/audits/2026-10-22-development-environment-readiness.md` | 历史保留 | 2026-10-22 环境就绪快照；目录入口已说明不代表当前主机状态，未重新验证当时证据 |
| `docs/audits/2026-10-22-speechnexus-design-review.md` | 历史保留 | 0.3.0 设计基线的审查与裁决记录；入口按审查记录引用，未重新验证当时证据 |
| `docs/audits/2026-10-23-architecture-contract-compliance.md` | 部分核对 | 核对文件清单及开头时点、基线和证据边界；正文及历史证据待逐项复核 |
| `docs/audits/2026-10-23-architecture-contract-post-refactor-review.md` | 部分核对 | 核对文件清单及开头时点、基线和证据边界；正文及历史证据待逐项复核 |
| `docs/audits/2026-10-23-architecture-contract-repair-closure.md` | 部分核对 | 核对文件清单及开头时点、基线和证据边界；正文及历史证据待逐项复核 |
| `docs/audits/2026-10-23-ui-ux-design-framework-audit.md` | 部分核对 | 核对文件清单及开头时点、基线和证据边界；正文及历史证据待逐项复核 |
| `docs/audits/2026-10-24-b00-megacube-environment-and-capability-gap.md` | 部分核对 | 核对文件清单及开头时点、基线和证据边界；正文及历史证据待逐项复核 |
| `docs/audits/2026-10-24-b01-default-recovery-driver.md` | 部分核对 | 核对文件清单及开头时点、基线和证据边界；正文及历史证据待逐项复核 |
| `docs/audits/2026-10-24-b01-recovery-progress.md` | 部分核对 | 核对文件清单及开头时点、基线和证据边界；正文及历史证据待逐项复核 |
| `docs/audits/2026-10-24-b02-controller-container-lifecycle-probe.md` | 部分核对 | 核对文件清单及开头时点、基线和证据边界；正文及历史证据待逐项复核 |
| `docs/audits/2026-10-24-b02-controller-http-shell-probe.md` | 部分核对 | 核对文件清单及开头时点、基线和证据边界；正文及历史证据待逐项复核 |
| `docs/audits/2026-10-24-b02-real-asr-controller-probe.md` | 部分核对 | 核对文件清单及开头时点、基线和证据边界；正文及历史证据待逐项复核 |
| `docs/audits/2026-10-24-b02-reconcile-proof-source.md` | 部分核对 | 核对文件清单及开头时点、基线和证据边界；正文及历史证据待逐项复核 |
| `docs/audits/2026-10-24-b02-stop-recovery-probe.md` | 部分核对 | 核对文件清单及开头时点、基线和证据边界；正文及历史证据待逐项复核 |
| `docs/audits/2026-10-24-b02-trusted-node-stop-probe.md` | 部分核对 | 核对文件清单及开头时点、基线和证据边界；正文及历史证据待逐项复核 |
| `docs/audits/2026-10-24-b02-warmup-gate-probe.md` | 部分核对 | 核对文件清单及开头时点、基线和证据边界；正文及历史证据待逐项复核 |
| `docs/audits/2026-10-24-b03-bounded-audio-probe.md` | 部分核对 | 核对文件清单及开头时点、基线和证据边界；正文及历史证据待逐项复核 |
| `docs/audits/2026-10-24-b03-file-asr-probe.md` | 部分核对 | 核对文件清单及开头时点、基线和证据边界；正文及历史证据待逐项复核 |
| `docs/audits/2026-10-24-b03-frozen-inputs.md` | 部分核对 | 核对文件清单及开头时点、基线和证据边界；正文及历史证据待逐项复核 |
| `docs/audits/2026-10-24-b03-native-asr-scope.md` | 部分核对 | 核对文件清单及开头时点、基线和证据边界；正文及历史证据待逐项复核 |
| `docs/audits/2026-10-24-b03-tts-adapter-probe.md` | 部分核对 | 核对文件清单及开头时点、基线和证据边界；正文及历史证据待逐项复核 |
| `docs/audits/2026-10-24-b03-tts-engine-feasibility.md` | 部分核对 | 核对文件清单及开头时点、基线和证据边界；正文及历史证据待逐项复核 |
| `docs/audits/2026-10-24-b06-cpu-vad-probe.md` | 部分核对 | 核对文件清单及开头时点、基线和证据边界；正文及历史证据待逐项复核 |
| `docs/audits/2026-10-24-ci-efficiency.md` | 部分核对 | 核对文件清单及开头时点、基线和证据边界；正文及历史证据待逐项复核 |
| `docs/audits/2026-10-25-b03-default-real-asr-probe.md` | 部分核对 | 核对标题、基线与历史时点边界；正文和原始证据待逐项复核 |
| `docs/audits/2026-10-25-b03-real-cancel-settlement.md` | 部分核对 | 核对标题、基线与历史时点边界；正文和原始证据待逐项复核 |
| `docs/audits/2026-10-25-b03-real-stop-settlement.md` | 部分核对 | 核对标题、基线与历史时点边界；正文和原始证据待逐项复核 |
| `docs/audits/2026-10-25-coordination-port-evidence.md` | 部分核对 | 核对标题、基线与历史时点边界；正文和原始证据待逐项复核 |
| `docs/audits/2026-10-25-team-c-next-phase-integration.md` | 部分核对 | 核对标题、基线与历史时点边界；正文和原始证据待逐项复核 |
| `docs/audits/2026-10-25-team-c-original-ac-matrix.md` | 部分核对 | 核对标题、基线与历史时点边界；正文和原始证据待逐项复核 |
| `docs/audits/2026-10-26-b03-default-real-tts-probe.md` | 部分核对 | 核对文件开头的历史日期、基线和结果边界；正文及原始证据待逐项复核 |
| `docs/audits/2026-10-26-b03-real-tts-stop-settlement.md` | 部分核对 | 核对文件开头的历史日期、基线和结果边界；正文及原始证据待逐项复核 |
| `docs/audits/2026-10-26-deployment-units-review.md` | 部分核对 | 核对文件开头的历史日期、基线和结果边界；正文及原始证据待逐项复核 |
| `docs/audits/2026-10-26-project-handoff.md` | 部分核对 | 核对文件开头的历史日期、基线和结果边界；正文及原始证据待逐项复核 |
| `docs/audits/2026-10-26-team-b-handoff.md` | 部分核对 | 核对文件开头的历史日期、基线和结果边界；正文及原始证据待逐项复核 |
| `docs/audits/2026-10-26-team-c-a11y-rerun.md` | 部分核对 | 核对文件开头的历史日期、基线和结果边界；正文及原始证据待逐项复核 |
| `docs/audits/2026-10-26-team-c-capture-cleanup.md` | 部分核对 | 核对文件开头的历史日期、基线和结果边界；正文及原始证据待逐项复核 |
| `docs/audits/2026-10-26-team-c-closeout-issue-audit.md` | 部分核对 | 核对文件开头的历史日期、基线和结果边界；正文及原始证据待逐项复核 |
| `docs/audits/2026-10-26-team-c-evidence-retention.md` | 部分核对 | 核对文件开头的历史日期、基线和结果边界；正文及原始证据待逐项复核 |
| `docs/audits/2026-10-26-team-c-input-device.md` | 部分核对 | 核对文件开头的历史日期、基线和结果边界；正文及原始证据待逐项复核 |
| `docs/audits/2026-10-26-team-c-operation-coverage.md` | 部分核对 | 核对文件开头的历史日期、基线和结果边界；正文及原始证据待逐项复核 |
| `docs/audits/2026-10-26-team-c-realtime-focus.md` | 部分核对 | 核对文件开头的历史日期、基线和结果边界；正文及原始证据待逐项复核 |
| `docs/audits/2026-10-26-team-c-realtime-integrity.md` | 部分核对 | 核对文件开头的历史日期、基线和结果边界；正文及原始证据待逐项复核 |
| `docs/audits/2026-10-26-team-c-remaining-gaps.md` | 部分核对 | 核对文件开头的历史日期、基线和结果边界；正文及原始证据待逐项复核 |
| `docs/audits/2026-10-26-team-c-render-loop-status.md` | 部分核对 | 核对文件开头的历史日期、基线和结果边界；正文及原始证据待逐项复核 |
| `docs/audits/2026-11-03-controlled-tts-prepare.md` | 部分核对 | 全文阅读并对照第六窗口证据文件及摘要，修正最新结论；各窗口原始事实尚未逐项核验 |
| `docs/audits/README.md` | 部分核对 | 全文阅读并修正历史交接入口、补充 TTS 审计入口；各历史摘要与原文待逐项核对 |
| `docs/components/README.md` | 已核对 | 核对索引中各目标文件、标题与候选状态；不代表目标正文已审 |
| `docs/components/audio-asset-inspection.md` | 历史保留 | 2026-10-26 候选与资格记录，保留原时点事实；当前入口的 TextAssetInspector 装配已对照源码，历史证据未重验 |
| `docs/components/binding-defaults-producer.md` | 部分核对 | 全文阅读并核对配置、Compose 透传、运行入口及契约入口；事务、HTTP 和验证细节待逐项核对 |
| `docs/components/binding-parameter-defaults.md` | 部分核对 | 定位 schema、HTTP 生产者与运行时装配；字段语义待逐项核对 |
| `docs/components/controlled-asr-generation.md` | 部分核对 | 局部对照代码、契约或历史证据；其余内容待核对 |
| `docs/components/cpu-vad.md` | 部分核对 | 局部对照代码、契约或历史证据；其余内容待核对 |
| `docs/components/execution-gateway.md` | 部分核对 | 核对 HTTP/装配章节与 drain 组合根；其余提交、屏障、恢复及清理事务待逐节核对 |
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
| `docs/components/repository-layout.md` | 部分核对 | 已对照工作区、迁移目录、接口目录、Compose 与验证入口修正布局；其余内容待核对 |
| `docs/components/runtime-controller-client.md` | 已核对 | 逐节静态对照客户端、节点观察循环、持久投递和测试清单；未运行服务 |
| `docs/components/synchronous-speech.md` | 已核对 | 逐节静态对照 TTS 装配、准入锁序、恢复、结算预检、配置和错误处理；未运行服务 |
| `docs/components/transcript-import-and-text-export.md` | 已核对 | 逐节静态对照来源校验、导入事务、编辑器、下载和公开契约；未运行服务 |
| `docs/components/trusted-node-stop.md` | 部分核对 | 局部对照代码、契约或历史证据；其余内容待核对 |
| `docs/components/workbench-content-downloads.md` | 已核对 | 逐节静态对照下载事务、HTTP、对象存储、浏览器消费者及公开契约；未运行服务 |
| `docs/components/workbench-export-integrity.md` | 已核对 | 逐节静态对照导出 worker、媒体校验、配置、组合根、持久写入及测试清单；未运行服务 |
| `docs/components/workbench-voice-catalogue.md` | 已核对 | 逐节静态对照公开契约、适配器、运行配置及 Web 消费者；未运行服务 |
| `docs/components/zero-dispatch-contract-core.md` | 部分核对 | 全文阅读并对照候选 schema、六个生成入口与索引；尚未逐项核对契约约束和历史接收证据 |
| `docs/decisions/0001-product-boundaries-and-authentication-scope.md` | 部分核对 | 全文阅读并对照 PRD 产品与认证边界；其他实现细节待核对 |
| `docs/decisions/0002-speech-models-engines-and-loading.md` | 待核对 | — |
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
| `docs/design/ui-ux-design-framework-and-tokens.md` | 待核对 | — |
| `docs/development-environment.md` | 部分核对 | 对照初始化、服务、浏览器冒烟和契约检查脚本，修正安装、权限、端口及检查边界；参考环境版本和历史运行状态未实测 |
| `docs/operations/README.md` | 已核对 | 核对目录定位、索引说明及所有本地链接目标；历史快照已标识 |
| `docs/operations/agent-delivery-sop.md` | 部分核对 | 全文阅读并对照根规约的授权与工作区收尾、验证指南入口；平台实际能力及需求关联待核对 |
| `docs/operations/backfill-binding-publication-evidence.md` | 已核对 | 对照 CLI、映射约束、回填事务、授权声明及 0021 回退锁；未连接生产库或运行回填 |
| `docs/operations/binding-activation-verification.md` | 部分核对 | 全文阅读并抽查 PG 并发和清理测试；未运行 PG 或核对全部矩阵 |
| `docs/operations/binding-disable-worker.md` | 已核对 | 对照配置范围、角色授权列、URL 策略、处理循环、异常分支及原租约结算入口；未连接部署或执行停用 |
| `docs/operations/binding-lifecycle.md` | 部分核对 | 全文阅读并核对授权、停用响应、主要操作与部分冲突分支；并发、审计和恢复事务待核对 |
| `docs/operations/builtin-voice-qualification.md` | 部分核对 | 核对受控窗口、资格入口、制品脚本摘要、主要授权及命令退出分支；probe 内部验证细节待核对 |
| `docs/operations/capacity-manifest.md` | 部分核对 | 核对聚合检查、占位值、校验器退出码及发布前后步骤；澄清 fixture、live 查询隔离与缺少工作区配额的范围，其余内容待核对 |
| `docs/operations/compose-minimal.md` | 部分核对 | 对照 Compose 服务、迁移链、配置准备、基础角色授权与查询代码，修正权限及失败边界并更新章节引用；未执行部署，停用 worker 和其他恢复细节待核对 |
| `docs/operations/file-asr-binding-handoff.md` | 部分核对 | 核对候选选型、离线制品与忽略元数据校验、工具退出和输出、网关输出格式及停止证据边界；其余内容待核对 |
| `docs/operations/file-asr-runtime.md` | 部分核对 | 对照配置、对象版本、worker 取消及结果登记恢复；停止及集成测试证据待核对 |
| `docs/operations/file-tts-binding-handoff.md` | 部分核对 | 核对候选参数、离线工具的忽略元数据与输出边界、后续 SDK 与租约结算证据；其余内容待核对 |
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
| `docs/operations/production-coordination-storage.md` | 部分核对 | 核对 0002 迁移、head、基础 DDL、授权生成器及释放守卫视图投影；全部字段待核对 |
| `docs/operations/production-runtime-delivery.md` | 已核对 | 对照配置、租约、unknown 路径、迁移、授权、ACK、Compose 及测试入口；未执行集成测试或验证目标部署 |
| `docs/operations/publish-binding-revision.md` | 部分核对 | 核对 CLI 观察、退出分类及 publication 与草稿检查顺序、发布意图重放和已有事实分支的 CAS 边界；其他事务分支及数据库实测待核对 |
| `docs/operations/qualification-report.md` | 部分核对 | 核对示例命令、场景状态、有效期及 limitations 校验边界，区分快照 fixture、离线脚本和产品组件的覆盖范围；其余内容待核对 |
| `docs/operations/reconciliation-recovery.md` | 部分核对 | 核对 abort 恢复查询、unknown 停止证据规则及查询 2、3 所涉主要表字段与证据标志类型；修正资源释放与预算归还顺序及代码锚点描述；查询执行及其余内容待核对 |
| `docs/operations/register-engine.md` | 部分核对 | 核对健康探测、注册事务、参数范围、配方覆盖规则及验证查询；补充提交结果未知时的恢复边界，其余内容待核对 |
| `docs/operations/release-preflight.md` | 已核对 | 对照脚本检查入口、配置加载和 C0–C12 判断；修正版本、身份、临时文件、资格场景、Compose 文本匹配及固定快照边界，未执行会访问服务或写入的检查 |
| `docs/operations/release-readiness.md` | 部分核对 | 全文阅读并抽核固定候选 planner、文件及提交关系；其余历史证据待复核 |
| `docs/operations/synchronous-transcription.md` | 部分核对 | 核对配置、配方约束、提交与轮询路径及网关主要错误映射；其他错误分支待核对 |
| `docs/plans/2026-10-22-speechnexus-implementation-plan.md` | 待核对 | — |
| `docs/plans/2026-10-23-agent-team-startup-briefs.md` | 部分核对 | 核对历史时点、现行入口及本地链接目标；历史计划和验收结果待复核 |
| `docs/plans/2026-10-23-architecture-contract-repairs.md` | 部分核对 | 核对历史时点、现行入口及本地链接目标；历史计划和验收结果待复核 |
| `docs/plans/2026-10-23-cluster-coordination-refactor.md` | 部分核对 | 核对历史时点、现行入口及本地链接目标；历史计划和验收结果待复核 |
| `docs/plans/2026-10-23-implementation-tracking.md` | 部分核对 | 核对历史时点、现行入口及本地链接目标；历史计划和验收结果待复核 |
| `docs/plans/2026-10-23-team-a-goal.md` | 部分核对 | 核对历史时点、现行入口及本地链接目标；历史计划和验收结果待复核 |
| `docs/plans/2026-10-23-team-b-goal.md` | 部分核对 | 核对历史时点、现行入口及本地链接目标；历史计划和验收结果待复核 |
| `docs/plans/2026-10-23-three-team-delivery-plan.md` | 部分核对 | 核对历史时点、现行入口及本地链接目标；历史计划和验收结果待复核 |
| `docs/plans/2026-10-24-a01-runtime-intent-unit.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-a02-asset-publication-unit.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-a02-binding-drafts-unit.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-b01-default-recovery-driver.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-b03-frozen-inputs.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-b03-native-asr-scope.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-2874-integration-handoff.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-7cd-integration-handoff.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-9ee-integration-handoff.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-a98-integration-handoff.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-c00-c1-handoff.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-c00-c1-plan.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-c00-deletion-handoff.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-c00-http-handoff.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-c00-http-plan.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-c00-listing-handoff.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-c00-listing-plan.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-c00-postgres-handoff.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-c00-project-boundary-handoff.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-c00-text-handoff.md` | 部分核对 | 全文阅读并标明历史时点；历史测试和后续实现未逐项复验 |
| `docs/plans/2026-10-24-team-c-c00-version-handoff.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-c01-events-handoff.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-c01-events-plan.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-c01-recovery-handoff.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-c01-recovery-plan.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-c01-task-center-handoff.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-c01-task-center-plan.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-c02-asset-reconcile-handoff.md` | 部分核对 | 全文阅读并标明历史时点；历史测试及后续实现未逐项复验 |
| `docs/plans/2026-10-24-team-c-c02-asset-reconcile-plan.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-c02-download-contract-handoff.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-c02-download-contract-plan.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-c02-file-flow-handoff.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-c02-host-handoff.md` | 部分核对 | 全文阅读并标明历史时点；历史测试及后续接入待复验 |
| `docs/plans/2026-10-24-team-c-c02-host-plan.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-c02-recovery-handoff.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-c02-recovery-plan.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-c02-text-export-handoff.md` | 部分核对 | 全文阅读并标记历史时点；历史测试及后续接入未逐项复验 |
| `docs/plans/2026-10-24-team-c-c02-text-export-plan.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-c02-transcript-import-handoff.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-c02-transcript-import-plan.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-c02-tts-handoff.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-c02-tts-host-handoff.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-c02-tts-host-plan.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-c02-tts-plan.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-c02-upload-recovery-handoff.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-c02-upload-recovery-plan.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-c04-capture-handoff.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-c04-capture-plan.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-c04-media-handoff.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-c05-prompter-handoff.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-c05-prompter-plan.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-c05-seal-handoff.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-c05-seal-plan.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-c06-llm-handoff.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-c06-llm-plan.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-c06-tasks-handoff.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-c06-tasks-plan.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-c07-render-handoff.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-c07-render-plan.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-c1-editor-handoff.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-c1-editor-plan.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-c1-management-handoff.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-c1-management-plan.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-deletion-ui-handoff.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-deletion-ui-plan.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-eb299-integration-handoff.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-goal.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-meeting-http-handoff.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-meeting-http-plan.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-meeting-ui-handoff.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-meeting-ui-plan.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-realtime-asr-handoff.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-realtime-asr-plan.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-realtime-tts-handoff.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-realtime-tts-plan.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-runtime-root-handoff.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-runtime-root-plan.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-teleprompter-persistence-handoff.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-teleprompter-persistence-plan.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-voice-create-handoff.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-voice-create-plan.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-workspaces-live-handoff.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-workspaces-live-plan.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-workspaces-read-handoff.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-24-team-c-workspaces-read-plan.md` | 待核对 | 已提取标题及开头供初筛，输出未完整审阅；全文和证据待核对 |
| `docs/plans/2026-10-25-b03-durable-file-runner.md` | 待核对 | — |
| `docs/plans/2026-10-25-b03-worker-fence.md` | 待核对 | — |
| `docs/plans/2026-10-25-ci-path-routing.md` | 待核对 | — |
| `docs/plans/2026-10-25-design-token-gate.md` | 待核对 | — |
| `docs/plans/2026-10-25-raster-gradient-contrast.md` | 待核对 | — |
| `docs/plans/2026-10-25-team-a-realignment.md` | 待核对 | — |
| `docs/plans/2026-10-25-team-c-admin-diagnostics-handoff.md` | 待核对 | — |
| `docs/plans/2026-10-25-team-c-assistant-honest-consumer-handoff.md` | 待核对 | — |
| `docs/plans/2026-10-25-team-c-assistant-honest-state-browser-handoff.md` | 待核对 | — |
| `docs/plans/2026-10-25-team-c-authoring-consumer-fix-handoff.md` | 待核对 | — |
| `docs/plans/2026-10-25-team-c-capability-report-handoff.md` | 待核对 | — |
| `docs/plans/2026-10-25-team-c-chapter-create-consumer-handoff.md` | 待核对 | — |
| `docs/plans/2026-10-25-team-c-export-production-prewiring-handoff.md` | 待核对 | — |
| `docs/plans/2026-10-25-team-c-issue-four-part-refresh-handoff.md` | 待核对 | — |
| `docs/plans/2026-10-25-team-c-keyboard-narrow-verification-handoff.md` | 待核对 | — |
| `docs/plans/2026-10-25-team-c-minutes-worker-handoff.md` | 待核对 | — |
| `docs/plans/2026-10-25-team-c-next-phase-goal-and-acceptance.md` | 待核对 | — |
| `docs/plans/2026-10-25-team-c-orphan-probes-gated-handoff.md` | 待核对 | — |
| `docs/plans/2026-10-25-team-c-project-rename-cas-handoff.md` | 待核对 | — |
| `docs/plans/2026-10-25-team-c-real-runtime-browser-handoff.md` | 待核对 | — |
| `docs/plans/2026-10-25-team-c-real-runtime-model-browser-handoff.md` | 待核对 | — |
| `docs/plans/2026-10-25-team-c-realtime-consent-browser-handoff.md` | 待核对 | — |
| `docs/plans/2026-10-25-team-c-realtime-evidence-promotion-handoff.md` | 待核对 | — |
| `docs/plans/2026-10-25-team-c-render-unit-create-consumer-handoff.md` | 待核对 | — |
| `docs/plans/2026-10-25-team-c-saved-asset-read-back-handoff.md` | 待核对 | — |
| `docs/plans/2026-10-25-team-c-teleprompter-state-consumer-handoff.md` | 待核对 | — |
| `docs/plans/2026-10-25-team-c-voice-revision-consumer-handoff.md` | 待核对 | — |
| `docs/plans/2026-10-25-web-accessibility-gate.md` | 待核对 | — |
| `docs/plans/2026-10-26-a02-audio-receipt-handoff.md` | 待核对 | — |
| `docs/plans/2026-10-26-a02-media-kernel-handoff.md` | 待核对 | — |
| `docs/plans/2026-10-26-ci-resource-partitions.md` | 待核对 | — |
| `docs/plans/2026-10-26-paperclip-bootstrap-plan.md` | 待核对 | — |
| `docs/plans/2026-10-26-project-manager-baseline.md` | 待核对 | — |
| `docs/plans/2026-10-26-team-a-final-handoff.md` | 待核对 | — |
| `docs/plans/2026-10-26-team-a-review-and-next-phase.md` | 待核对 | — |
| `docs/plans/2026-10-26-team-c-final-handoff.md` | 待核对 | — |
| `docs/plans/2026-10-26-team-c-next-phase-execution-and-collaboration.md` | 待核对 | — |
| `docs/plans/2026-10-27-delegra-legion-closed-loop.md` | 待核对 | — |
| `docs/plans/2026-10-30-zero-dispatch-settlement.md` | 待核对 | — |
| `docs/plans/README.md` | 部分核对 | 核对历史索引定位及 77 个本地链接目标；历史摘要尚未逐项核实 |
| `docs/plans/binding-disable-cancellation.md` | 待核对 | — |
| `docs/plans/delivery-tracking.md` | 待核对 | — |
| `docs/po-sm-operating-playbook.md` | 部分核对 | 核对 fleet-status 参数及退出码；其他章节与数据库行为待核对 |
| `docs/prd.md` | 部分核对 | 核对 65 个需求 ID、锚点和 Epic 与结构化清单一致；逐项验收语义待核对 |
| `docs/project-manager-guide.md` | 待核对 | — |
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
| `docs/architecture/diagrams/deployment-clusters-v3-4k/01-cluster-overview-4k.check.json` | 部分核对 | JSON 语法、重复键及非有限数值检查通过；来源与语义待核对 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/01-cluster-overview-4k.content.json` | 部分核对 | JSON 语法、重复键及非有限数值检查通过；来源与语义待核对 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/01-cluster-overview-4k.png` | 待核对 | 非 Markdown 制品；须按用途核对来源、内容与引用 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/01-cluster-overview-4k.preview.png` | 待核对 | 非 Markdown 制品；须按用途核对来源、内容与引用 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/01-cluster-overview-4k.svg` | 待核对 | 非 Markdown 制品；须按用途核对来源、内容与引用 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/02-model-instance-topology-4k.check.json` | 部分核对 | JSON 语法、重复键及非有限数值检查通过；来源与语义待核对 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/02-model-instance-topology-4k.content.json` | 部分核对 | JSON 语法、重复键及非有限数值检查通过；来源与语义待核对 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/02-model-instance-topology-4k.png` | 待核对 | 非 Markdown 制品；须按用途核对来源、内容与引用 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/02-model-instance-topology-4k.preview.png` | 待核对 | 非 Markdown 制品；须按用途核对来源、内容与引用 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/02-model-instance-topology-4k.svg` | 待核对 | 非 Markdown 制品；须按用途核对来源、内容与引用 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/assets/application.png` | 待核对 | 非 Markdown 制品；须按用途核对来源、内容与引用 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/assets/compute.png` | 待核对 | 非 Markdown 制品；须按用途核对来源、内容与引用 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/assets/data.png` | 待核对 | 非 Markdown 制品；须按用途核对来源、内容与引用 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/build_diagrams.py` | 待核对 | 非 Markdown 制品；须按用途核对来源、内容与引用 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/image-prompts.json` | 部分核对 | JSON 语法、重复键及非有限数值检查通过；来源与语义待核对 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/model-catalog.json` | 部分核对 | JSON 语法、重复键及非有限数值检查通过；来源与语义待核对 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/render_and_validate.cjs` | 待核对 | 非 Markdown 制品；须按用途核对来源、内容与引用 |
| `docs/architecture/diagrams/deployment-clusters-v3-4k/validation.json` | 部分核对 | JSON 语法、重复键及非有限数值检查通过；来源与语义待核对 |
| `docs/audits/2026-10-25-team-c-acceptance-map.json` | 部分核对 | JSON 语法、重复键及非有限数值检查通过；来源与语义待核对 |
| `docs/audits/2026-10-26-team-a-closeout-evidence.json` | 部分核对 | JSON 语法、重复键及非有限数值检查通过；来源与语义待核对 |
| `docs/audits/2026-10-26-team-a-issue-handoff.json` | 部分核对 | JSON 语法、重复键及非有限数值检查通过；来源与语义待核对 |
| `docs/audits/2026-10-26-team-c-operation-coverage.json` | 部分核对 | JSON 语法、重复键及非有限数值检查通过；来源与语义待核对 |
| `docs/audits/evidence/2026-11-03-tts-prepare.json` | 部分核对 | JSON 语法、重复键及非有限数值检查通过；来源与语义待核对 |
| `docs/audits/evidence/2026-11-03-tts-window-2.json` | 部分核对 | JSON 语法、重复键及非有限数值检查通过；来源与语义待核对 |
| `docs/audits/evidence/2026-11-03-tts-window-3.json` | 部分核对 | JSON 语法、重复键及非有限数值检查通过；来源与语义待核对 |
| `docs/audits/evidence/2026-11-03-tts-window-4.json` | 部分核对 | JSON 语法、重复键及非有限数值检查通过；来源与语义待核对 |
| `docs/audits/evidence/2026-11-03-tts-window-5.json` | 部分核对 | JSON 语法、重复键及非有限数值检查通过；来源与语义待核对 |
| `docs/audits/evidence/2026-11-03-tts-window-6.json` | 部分核对 | JSON 语法、重复键及非有限数值检查通过；来源与语义待核对 |
| `docs/operations/release-evidence-verification-output.txt` | 历史保留 | 2026-10-28 固定版本的合成数据验证输出；不代表当前发布资格，原始运行未重做 |
| `docs/plans/2026-10-23-implementation-issues.json` | 部分核对 | JSON 语法、重复键及非有限数值检查通过；来源与语义待核对 |
| `docs/plans/2026-10-24-team-c-c02-download-contract-candidate.yaml` | 部分核对 | 全文阅读，确认标注为候选并有专门的契约示例；尚未核验所有引用与当前契约差异 |
| `docs/plans/2026-10-24-team-c-gateway-clock-candidate.patch` | 待核对 | 非 Markdown 制品；须按用途核对来源、内容与引用 |
| `docs/plans/delivery-issues.json` | 部分核对 | JSON 语法、重复键及非有限数值检查通过；来源与语义待核对 |
