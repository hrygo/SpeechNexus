---
title: 容量 manifest（发布证据）
status: active
version: "1.0.0"
date: 2026-10-28
---

# 容量 manifest（发布证据）

`capacity_manifest` 是绑定 revision 的**容量与执行配方声明**，不是监控快照，也不是模型质量报告。它把“这个不可变 binding revision 需要什么资源、在哪个池和拓扑上运行、允许并发多少、如何取消和 fencing、由哪份资格报告支撑”固定成一份可引用、可摘要的证据。发布命令必须同时收到 manifest id 与 qualification report id；定义里的 `execution_recipe.capacity_manifest_id` 又必须与前者完全相等，否则发布在写库前以退出码 `6` 拒绝。

本文回答四个问题：

1. 每个字段是什么、由谁提供、如何核验；
2. manifest 与 `engine_instances`、`resource_pools`、`host_nodes` 的容量账本如何一致；
3. 一份真实样例如何从测量记录走到可发布证据；
4. 一个没参与过本项目的人，如何独立产出下一份合法 manifest。

本文只定义和核验证据，不修改冻结契约、不修改产品代码，也不把 `local-test-capacity-1` 一类占位值当作真实容量。样例使用的 `local-test-*` 只允许出现在隔离测试里；发布路径上的校验脚本会直接拒绝它。

## 1. 输入、输出与权威位置

| 角色 | 路径 / 表 | 说明 |
| --- | --- | --- |
| 冻结形状 | `contracts/capacity-manifest.schema.json` | JSON Schema 1.0.0；本文不复制全部约束，只解释运维必须填对的字段 |
| 冻结形状 | `contracts/qualification-report.schema.json` | 报告形状；manifest 的 `qualification` 必须指向它的 `report_id` |
| 模型与引擎 pin | `deploy/engines/file-asr-audio/artifact-pin.json` | 模型文件、模型卡、VAD 与运行时 pin；`model` 对象的 RFC 8785 摘要就是 stage 的 `model_revision_digest` |
| binding 定义 | `deploy/engines/file-asr-audio/binding-definition.json` | 当前仓库随附定义，`execution_recipe` 尚缺 `capacity_manifest_id`；不能直接拿它发布真实 manifest |
| 样例 manifest | `deploy/engines/file-asr-audio/evidence/gb10-20261027/capacity-manifest.json` | 本文的真实数值样例；release tier 为 `experimental` |
| 样例报告 | `deploy/engines/file-asr-audio/evidence/gb10-20261027/qualification-report.json` | 与样例 manifest 配对；当前 `overall_status=incomplete` |
| 候选定义 | `deploy/engines/file-asr-audio/evidence/gb10-20261027/binding-definition.candidate.json` | 只用于证明“未来 revision 应如何声明”，不是已部署定义，也不是对历史定义的修改 |
| 原始证据 | `deploy/engines/file-asr-audio/evidence/gb10-20261027/evidence.json` | #243 comment `6453106316` 的结构化转述；逐 scenario 摘要的输入 |
| 数据库快照 fixture | `tests/fixtures/release-evidence/gb10-20261027-db-snapshot.json` | 只证明查询路径与跨表规则，不是 live DB、不是发布回执 |
| 校验脚本 | `scripts/dev-env/verify-release-evidence.py` | 只读；校验 schema、跨文档、摘要、DB/快照容量账本 |

**当前基线**：仓库 `main` 的完整 base SHA 为 `9d1b9bbdc1041bfb7c33505a4788490c1db5ea45`。样例中的模型、镜像和测量数值来自 #243 的 GPU 岗记录与仓库 artifact pin；本任务没有下载模型、没有连接 GPU、没有重跑测量。

## 2. 顶层字段表

“谁提供”是职责，不是授权：实际写入必须服从当前任务的唯一写者范围和发布授权。

| 字段 | 必填 | 含义 | 谁提供 | 如何核验 |
| --- | --- | --- | --- | --- |
| `schema_version` | 是 | 固定为 `"1.0.0"`；形状版本，不是产品版本 | 证据产出人 | JSON Schema；不得自造小版本 |
| `manifest_id` | 是 | 本 manifest 的不可变 ID；发布证据和定义都引用它 | 证据产出人，命名由 binding/平台约定 | `shared/identifiers.schema.json` 的 `Identifier`；全链路相等；拒绝 `local-test-*` |
| `binding_revision_id` | 是 | 被治理的不可变 binding revision 的 `revision_id` | binding 草稿/发布系统 | 必须等于 `speech_binding_revisions.revision_id`、报告同名字段、publication/evidence 行、`engine_instances.binding_revision_id` |
| `pool_id` | 是 | 逻辑能力池；不是主机名 | 平台/容量负责人 | 枚举 `asr-realtime`、`tts-realtime`、`asr-batch`、`tts-batch`、`voice-creation`、`auxiliary`；必须存在于 `resource_pools` 且与 instance 一致 |
| `service_class` | 是 | 池的服务等级：`realtime` / `interactive` / `batch` | 产品与平台共同确定 | 必须等于 `resource_pools.service_class` 与 instance 所在池 |
| `management_mode` | 是 | `managed` 需要独占网关入口、完整池记账和 fencing/abort；`external` 不能声明生产实时 SLO | 平台负责人 | Schema 约束：`external` 只能是 `best_effort` 或 `experimental` |
| `release_tier` | 是 | `experimental` / `best_effort` / `production` | 产品负责人；最终由唯一 SM+PO 放行 | 必须与报告 `release_tier` 相等；`production` 还要求报告 `overall_status=pass` 且有 `expires_at` |
| `loading_policy` | 是 | `resident` 或 `on_demand`；影响加载峰值、回收冷却与准入 | 平台/引擎负责人 | 与 `engine_instances.loading_policy` 相等；`on_demand` 必须给出 `idle_reclaim_cooldown_ms >= 1000` |
| `topology_ref` | 是 | 逻辑节点 profile、内存架构、共享父预算和拓扑摘要 | 平台负责人 | 见 §5；必须与报告 `topology`、`host_nodes.topology_digest` 对齐 |
| `supported_tasks` | 是 | 该 revision 接受的任务，例如 `asr_file` | 产品/定义作者 | 必须是定义 `tasks` 的子集；当前样例只有 `asr_file` |
| `supported_input_formats` | 是 | 接受的输入容器/采样格式 | 产品/引擎负责人 | Schema 枚举；必须与真实引擎能力一致，不能把“计划支持”写成“已支持” |
| `supported_output_formats` | 是 | 接受的输出格式 | 产品/引擎负责人 | Schema 枚举；若 stage `emits_audio_pcm=true`，必须包含 PCM/WAV 等音频格式 |
| `stage_graph` | 是 | 完整执行阶段图；每个引擎/模型阶段一条 | 模型/引擎负责人，平台补充资源 | 见 §3；stage id 必须与报告 `software.engine_components` 一一对应 |
| `resource_envelope` | 是 | 由 `stage_graph` 聚合出的准入包络 | 证据产出人用脚本生成 | 见 §3；不得手填与 stage 不一致的数 |
| `rate_and_segment_limits` | 是 | 速率、segment、渲染单元和硬输出配额 | 产品/引擎负责人 | 与定义的 `parameter_domains.asr.max_segment_ms` 一致；引擎是否真的硬限制要单独取证 |
| `cancellation_and_fencing` | 是 | 取消模式、是否支持 abort/fencing、最坏不可抢占时长、drain 与回收冷却 | 引擎/平台实测负责人 | 必须有实际取消/隔离证据；样例只有 `connection_drop_with_quarantine`，不能声明 abort/fencing |
| `qualification` | 是 | 指向资格报告、时间、配置包摘要和已验证共存 profile | 证据产出人与资格报告作者 | 报告 id、时间、`config_bundle_digest` 必须与报告/文件一致；profile 必须有对应实验记录 |

## 3. stage graph、六维资源与包络

### 3.1 每个 stage 的字段

| 字段 | 含义 | 谁提供 | 如何核验 |
| --- | --- | --- | --- |
| `stage_id` | 阶段稳定 ID；报告用它关联引擎组件 | 模型/引擎负责人 | 与报告 `engine_components` 的 key 完全相等 |
| `stage_role` | `vad`、`asr_preview`、`asr_review`、`asr_batch`、`tts_talker`、`tts_code2wav`、`tts_connector`、`voice_design`、`voice_clone_base`、`alignment`、`diarization` | 架构/模型负责人 | 与定义 `execution_recipe.stage_roles` 的集合相等 |
| `engine_kind` | `vllm`、`vllm_omni`、`nemo`、`qwen_forced_aligner`、`onnx_runtime`、`cpu_native` | 引擎负责人 | 与报告同名 stage 的 `engine_kind` 相等 |
| `model_revision_digest` | 该 stage 模型/权重的 RFC 8785 SHA-256 | 模型 pin 负责人 | ASR stage 必须等于 `artifact-pin.json` 中 `model` 对象的 RFC 8785 摘要；VAD/对齐等 stage 用各自 artifact 的摘要 |
| `required_for_admission` | 是否在准入时原子预留该 stage | 架构/平台负责人 | 不能把必需 stage 标 false 来绕过预算；报告必须覆盖该 stage |
| `emits_audio_pcm` | 是否输出 PCM；输出计量按此 stage 计 | 引擎负责人 | 只有真正输出音频的 stage 为 true |
| `resident_budget` | 常驻（steady）预算，每个运行实例计一次 | 引擎/平台实测或声明 | 六维 + `engine_active_slots`；见 §3.2 |
| `loading_peak_budget` | 加载、warmup、handoff 峰值 | 引擎/平台实测或声明 | 不得低于 `resident_budget` 的同一维度；见 §6 |
| `per_slot_incremental_budget` | 每个活跃执行 slot 的增量上界 | 引擎/平台实测或声明 | 与 `max_active_slots` 一起决定主机余量 |
| `max_active_slots` | 该 stage 允许的最大并发 slot | 引擎/平台容量负责人 | 包络取所有 stage 的最小值 |

### 3.2 六个物理维度和三个计量维度

`ResourceVector` 有九个数值键；任务所说的“六维”是前六个物理资源，后三个是带宽/对象存储计量。所有值都是非负整数，单位在键名里固定。

| 键 | 含义 | 谁提供 | 如何核验 |
| --- | --- | --- | --- |
| `unified_physical_memory_bytes` | 统一内存架构下计入父物理预算的总内存；避免 host/device 双计 | 平台/引擎实测 | 与 `host_nodes.allocatable_unified_physical_memory_bytes`、instance `*_unified_...` 对账 |
| `device_memory_bytes` | 离散设备显存；统一内存架构通常为 0 | 平台/引擎实测 | 与 `host_nodes.allocatable_device_memory_bytes` 对账；统一架构必须说明为何非 0（如有） |
| `host_memory_bytes` | 主机 RAM；统一架构通常并进 unified | 平台/引擎实测 | 与 `host_nodes.allocatable_host_memory_bytes` 对账 |
| `cpu_decode_millicores` | CPU 解码/前后处理毫核 | 平台实测或声明 | 与 host CPU allocatable/held 对账；样例是声明值，不是实测 |
| `host_pcm_buffer_bytes` | 主机 PCM 缓冲上界 | 引擎/平台实测或声明 | 与 host PCM allocatable/held 对账；样例是声明值 |
| `scratch_disk_bytes` | 临时盘上界 | 平台/引擎实测或声明 | 与 host scratch allocatable/held 对账；样例声明 0，表示本实验不使用 scratch |
| `ingress_bandwidth_bps` | 入向带宽上界（字节/秒） | 平台/网关 | 报告/监控核对；当前没有独立 host 账本列 |
| `egress_bandwidth_bps` | 出向带宽上界（字节/秒） | 平台/网关 | 报告/监控核对；当前没有独立 host 账本列 |
| `object_storage_bytes` | 对象存储上界 | 平台/对象存储负责人 | 与资产保留/配额策略核对；当前没有独立 host 账本列 |

`engine_active_slots` 是“每个端到端执行在该 stage 占用多少 slot”的基数，不是资源字节；聚合规则见下。

### 3.3 包络聚合规则（必须逐字实现）

对 `resident_budget`、`loading_peak_budget`、`per_slot_incremental_budget` 三组向量：

1. 九个数值键分别对所有 stage **求和**；
2. `engine_active_slots` 取所有 stage 的 **最大值**（一次端到端执行在每个 stage 各占一个 slot，不能相加）；
3. `resource_envelope.max_active_slots` 取所有 stage `max_active_slots` 的 **最小值**（可选 stage 也约束，因为它可能执行）；
4. `resource_envelope.realtime_reserved_slots` 必须 `<= max_active_slots`；
5. 任一 stage 的 `loading_peak_budget` 在同一维度不得低于 `resident_budget`；`per_slot` 是增量，不是峰值替代品。

脚本 `check_envelope` 会重算上述五项；任何一项不一致即 `FAIL`。不要手改 `resource_envelope` 去“修”stage，stage 才是事实源。

## 4. 许可、配额、生效期、适用 node/pool

### 4.1 许可（license）

`capacity-manifest.schema.json` **没有** license 字段。许可不是可以随手加进 JSON 的自由字段；当前权威位置是 artifact pin：

| 证据 | 值 | 提供方 | 核验方式 |
| --- | --- | --- | --- |
| `artifact-pin.json` → `model.license` | 样例为 `Apache-2.0` | 模型 pin 负责人 | 读取 pin；与模型卡/模型仓库声明核对；许可证接受记录属于发布前检查，不在 schema 内 |
| `artifact-pin.json` → `model.model_card.digest` | 模型卡文件摘要 | 模型 pin 负责人 | 与模型卡原始字节核对 |
| `artifact-pin.json` → `vad.license` | 样例为 `MIT` | 资产 pin 负责人 | 仅当 VAD 进入 stage graph 时才作为该 stage 的 artifact 证据 |
| `artifact-pin.json` → `model.files[].artifact.digest` | 每个权重/配置/词表文件摘要 | 模型 pin 负责人 | 与下载/制品存储中的字节核对；本任务未下载模型，不能声称已重算这些字节 |

如果产品要求“许可证审批”成为机器门禁，应新增独立证据字段或发布检查卡；不要把它塞进 manifest 的 `additionalProperties: false` 对象，也不要用注释伪装成字段。

### 4.2 配额（quota）

manifest 本身只有“每个实例/每个 slot 的包络”，没有“池总配额”字段。真实账本分三层：

| 层 | 表 / 字段 | 作用 | 当前事实 |
| --- | --- | --- | --- |
| 主机物理预算 | `host_nodes.allocatable_*`、`held_*`、`quarantined_*` | 准入按 per-slot 向量从 allocatable 扣除 held/quarantine；prepare 按 peak 预留 | 六个物理维度有列；带宽/对象存储没有 host 列 |
| 实例 slot | `engine_instances.max_slots`、`held_slots`、`quarantined_slots`、`realtime_reserved_slots` | 限制单实例并发、隔离占用和实时保留 | `held + quarantined <= max_slots`；非实时服务还要给 realtime 保留让路 |
| 工作区尝试 | `workspace_quotas.max_attempts`、`held_attempts`、`quarantined_attempts` | 限制一个 workspace 的并发 attempt | `held + quarantined <= max_attempts` |
| 池 | `resource_pools.pool_id`、`service_class`、`pool_state` | 逻辑池身份与服务等级 | **没有数值 quota 列**；不能从 `resource_pools` 查出一个“池容量上限” |

因此：

- “池配额”目前不是一个可核验的数值；`resource_pools` 只证明池存在、服务等级匹配、池状态为 active。脚本对此明确输出 `NA`，而不是编造一个通过。
- 真正的超限发生在主机向量、实例 slot 或 workspace attempt 三层；
- 需要池级总量配额时，应单独立卡增加表/列与准入逻辑，不能在文档里假装已有。

### 4.3 生效期

| 字段 | 含义 | 谁提供 | 核验 |
| --- | --- | --- | --- |
| `qualification.qualified_at` | 资格报告完成时间 | 测量/证据负责人 | 必须等于报告 `qualified_at`，且早于 `expires_at` |
| `qualification.expires_at` | 证据失效时间 | 证据负责人按组织政策设定 | 必须等于报告 `expires_at`；当前时间超过即 `FAIL`；`production` 不得为空 |
| `qualification.config_bundle_digest` | 本次资格所固定的 artifact pin + binding definition 的 RFC 8785 摘要 | 证据产出人 | 见 §7；任何一边改动都必须重算并重新资格 |
| `qualification.verified_coexistence_profiles` | 已验证的共存 profile | 平台/实验负责人 | 每个 profile 必须有对应实验记录；不能只写名字 |

样例使用 90 天有效期：`2026-10-27T20:44:19Z` → `2027-01-25T20:44:19Z`。90 天是样例策略，不是 schema 常量；组织可以更短，但不能让生产证据永不过期。

### 4.4 适用 node / pool

| 概念 | manifest / 报告字段 | 数据库 | 规则 |
| --- | --- | --- | --- |
| 逻辑池 | `pool_id`、`service_class` | `resource_pools.pool_id`、`service_class`、`pool_state` | 必须存在、active、service class 相等 |
| 逻辑节点 profile | `topology_ref.host_node_profile_id` | 无直接同名列 | 描述硬件类别；不是 `host_node_id`，不能拿它当外键 |
| 实际主机 | 报告 `topology.host_node_id` | `host_nodes.host_node_id` | instance 的 `host_node_id` 必须指向该行；节点必须 `active` |
| 拓扑摘要 | `topology_ref.topology_digest` | `host_nodes.topology_digest` | 必须相等；摘要描述 CPU 架构、硬件类别、内存架构、总物理内存 |
| 内存架构 | `topology_ref.memory_architecture` | `host_nodes.memory_architecture` | 必须相等；统一内存必须声明 `requires_shared_parent_budget=true` |

样例的逻辑 profile 是 `gb10-cube-ccba-unified-121g`，实际报告主机是 `cube-ccba`；两者不是同一个标识空间。profile 说明“哪类机器”，`host_node_id` 说明“哪台机器”，`topology_digest` 把两者绑定到同一份硬件描述。

## 5. 与数据库容量向量的一致性规则

以下规则是发布/准入前的硬核对。脚本实现了 R1–R12；其中 R1–R6 在 fixture 或 live DB 上都能跑，R7–R12 需要对应行存在。

| 编号 | 规则 | 失败表现 |
| --- | --- | --- |
| R1 | `manifest.binding_revision_id` = 报告同名字段 = `speech_binding_revisions.revision_id` = `speech_publications.revision_id` = `speech_binding_publication_evidence.revision_id` = 每个 `engine_instances.binding_revision_id` | `FAIL`，不得发布 |
| R2 | `manifest.manifest_id` = `binding_definition.execution_recipe.capacity_manifest_id` = evidence `capacity_manifest_id` = 每个 `engine_instances.manifest_id` | 发布 CLI 退出码 `6`；脚本 `FAIL` |
| R3 | `report.report_id` = `manifest.qualification.qualification_report_id` = evidence `qualification_report_id` | `FAIL` |
| R4 | `manifest.release_tier` = 报告 `release_tier` = 定义 `release_tier`（定义有该字段时）；`production` 还要求报告 `pass` | `FAIL`；`external` + `production` 直接被 schema 拒绝 |
| R5 | 报告 `engine_components` 的 key 集合 = `stage_graph` 的 `stage_id` 集合；每个 `engine_kind` 相等 | `FAIL` |
| R6 | 每个 ASR stage 的 `model_revision_digest` = `artifact-pin.json.model` 的 RFC 8785 摘要 | `FAIL` |
| R7 | `engine_instances` 的 `peak_*`、`steady_*`、`per_slot_*` 六维与 manifest 三组向量逐项相等；`max_slots`、`realtime_reserved_slots` 相等；`management_mode`、`loading_policy`、`pool_id`、`manifest_id`、`binding_revision_id` 相等 | `FAIL` |
| R8 | instance 必须 `instance_state=ready`、`base_budget_kind=steady`、`stopped_at_us IS NULL`；`held_slots + quarantined_slots <= max_slots` | `FAIL` |
| R9 | instance `config_digest` = manifest `qualification.config_bundle_digest` | `FAIL` |
| R10 | `host_nodes.node_state=active`；`memory_architecture`、`topology_digest` 与 manifest 相等 | `FAIL` |
| R11 | 对每个物理维度：`held + quarantined <= allocatable`；`held >= instance.peak`（host 已为峰值预留）；`peak <= allocatable`；剩余 slot 的 `per_slot * remaining_slots <= allocatable - held - quarantined` | `FAIL` |
| R12 | `workspace_quotas.held_attempts + quarantined_attempts <= max_attempts`；`resource_pools.pool_state=active`、`service_class` 相等 | `FAIL` |

脚本对 `resource_pools` 数值配额输出 `NA`（见 §4.2），不是通过；对 `database.source=fixture` 输出 `WARN`，因为 fixture 只证明查询路径，不证明真实发布或真实容量。

### 谁能超、超了报什么

没有任何角色被授权“超卖”。只能通过新的 manifest/report 和新 revision 扩大声明，并先扩大 `host_nodes.allocatable_*`；不能改旧证据。

| 超限点 | 代码行为 | 对外错误 |
| --- | --- | --- |
| 准入按 per-slot 向量预留，`allocatable - held - quarantined` 不足 | `reserve_resources` 抛 `CapacityExceededError`，准入层转换 | `AdmissionRejectedError("capacity_exhausted")` |
| 实例 slot：`held + quarantined + extra > max_slots`，或非实时请求挤压 `realtime_reserved_slots` | 准入层显式检查 | `AdmissionRejectedError("capacity_exhausted")` |
| 运行 prepare 按 `plan.peak` 预留主机不足 | `reserve_resources` 抛 `CapacityExceededError`，运行层转换 | `RuntimeRejectedError("capacity_exhausted")` |
| workspace 并发 attempt 达到 `max_attempts` | `held + quarantined >= max_attempts` | `AdmissionRejectedError("quota_exceeded")` |
| 拓扑/内存架构/stage 与批准 recipe 不一致 | 准入层检查 | `AdmissionRejectedError("recipe_mismatch")` |
| instance 非 ready/steady、host 非 active、runtime 未 prepare、池 class 不符 | 准入层检查 | `AdmissionRejectedError("policy_or_resource_disabled")` |
| 定义 `capacity_manifest_id` 缺失/不等于发布参数 | 发布事务写前检查 | CLI 退出码 `6`，`evidence mismatch: ...` |
| manifest/report/schema/摘要/DB 账本不一致 | 校验脚本 | `RESULT FAIL`，退出码 `1`（只读，不写库） |
| 池级总量 | 当前无字段、无逻辑 | 不可核验；`capacity.pool_quota=NA`，需要后续卡 |

注意：代码执行的是**声明向量**，不是物理遥测。声明值可以低于实际峰值而暂时“通过”；这类偏差要靠测量、监控和失效流程发现，不能把“准入通过”当成“物理容量已证明”。

## 6. 产出、核验、变更与失效

### 6.1 角色

| 角色 | 职责 | 不能做什么 |
| --- | --- | --- |
| 测量/平台执行人 | 在批准的隔离环境运行模型/实例，提供原始命令、输出、环境、资源观测 | 不能把未测值写成实测；不能直接自签发布 |
| manifest 产出人 | 把测量与 pin 整理成 stage graph、向量、限额、取消能力 | 不能改冻结 schema；不能伪造 host/instance 行 |
| 非作者证据核验人 | 核对 schema、摘要、跨文档、DB 账本、样本来源 | 不能代替作者改实现；不能代替 SM+PO 放行 |
| 高风险审查者 | 对协议、原子事务、恢复、fencing、停止/预算释放等高风险改变做非作者审查与环境验证 | 不能把普通文档审查当作高风险验证 |
| 唯一 SM+PO | 决定产品目标、优先级、范围、AC，批准发布/投产 Gate | 不能跳过证据直接让占位值进真实路径 |
| 发布操作员 | 用批准的 DSN 和命令发布已存在的 revision，带 manifest/report id 与摘要 | 不能创建/修改 revision；不能把发布成功当成生产资格 |

当前部署中，本证据岗承办格式、样例与核验；最终发布放行仍由唯一 SM+PO 负责。参与候选实施的人不能换角色自签。

### 6.2 从零产出一份合法 manifest

1. **固定模型与引擎**：更新/确认 `artifact-pin.json` 的 `model`（repository、revision、license、model_card、files）与 engine pin；不要用浮动 tag。
2. **拿完整 revision id**：从 binding 草稿/发布系统取得不可变的 `binding_revision_id`；manifest 不能先于 revision 编造一个。
3. **列 stage**：按真实执行路径列全 stage；文件 ASR 当前样例只有 `asr_batch`。若 VAD、对齐、说话人分离实际参与，必须各自成 stage，不能藏在 `parameter_defaults` 里。
4. **填三组向量**：每个 stage 填 `resident`、`loading_peak`、`per_slot`；每个值标注“实测 / 声明上界 / 未知”。未知的 CPU/PCM/scratch/带宽不能写成 0 冒充实测；要么给保守声明并注明，要么阻塞该项。
5. **聚合**：用脚本或 §3.3 规则生成 `resource_envelope`；不要手填。
6. **填限额与取消**：`max_segment_audio_ms` 与定义 `parameter_domains.asr.max_segment_ms` 对齐；`hard_output_quota` 只在引擎真的先验限流时为 true；取消/fencing 只在有证据时为 true。
7. **写资格引用**：填入配对报告的 `report_id`、`qualified_at`、`expires_at`、`config_bundle_digest`、已验证共存 profile。
8. **静态校验**：跑 JSON Schema 与 `verify-release-evidence.py`（至少 `--database-snapshot` 或 live DB）；先让所有 `FAIL` 清零，`NA/WARN` 逐条解释。
9. **登记数据库**：新 revision 的 `definition_json` 写入 `execution_recipe.capacity_manifest_id`；`engine_instances` 三组向量/`max_slots`/`config_digest` 与 manifest 一致；`host_nodes` 的 allocatable/held/quarantine 反映真实预算；`resource_pools` active；`workspace_quotas` 有界。
10. **发布**：用 `speechnexus-publish-binding` 传入 manifest/report id 与两个摘要；保存命令输出、退出码与 publication/evidence 行。发布是 insert-only，旧 revision 不修改。

### 6.3 模板（把 `<...>` 换成真实值）

```json
{
  "schema_version": "1.0.0",
  "manifest_id": "capm-<binding>-<tier>-<yyyymmdd>",
  "binding_revision_id": "mbrev-<binding>-<...>",
  "pool_id": "asr-batch",
  "service_class": "batch",
  "management_mode": "external",
  "release_tier": "experimental",
  "loading_policy": "resident",
  "topology_ref": {
    "host_node_profile_id": "<logical-profile>",
    "memory_architecture": "unified_memory",
    "requires_shared_parent_budget": true,
    "topology_digest": "sha256:<topology-digest>"
  },
  "supported_tasks": ["asr_file"],
  "supported_input_formats": ["wav", "pcm_s16le_16k_mono"],
  "supported_output_formats": ["utf8_text", "json_segments"],
  "stage_graph": [
    {
      "stage_id": "stage-<engine>",
      "stage_role": "asr_batch",
      "engine_kind": "vllm",
      "model_revision_digest": "sha256:<artifact-pin-model-digest>",
      "required_for_admission": true,
      "emits_audio_pcm": false,
      "resident_budget": {"engine_active_slots": 0, "device_memory_bytes": 0, "host_memory_bytes": 0, "unified_physical_memory_bytes": 0, "cpu_decode_millicores": 0, "host_pcm_buffer_bytes": 0, "scratch_disk_bytes": 0, "ingress_bandwidth_bps": 0, "egress_bandwidth_bps": 0, "object_storage_bytes": 0},
      "loading_peak_budget": {"engine_active_slots": 0, "device_memory_bytes": 0, "host_memory_bytes": 0, "unified_physical_memory_bytes": 0, "cpu_decode_millicores": 0, "host_pcm_buffer_bytes": 0, "scratch_disk_bytes": 0, "ingress_bandwidth_bps": 0, "egress_bandwidth_bps": 0, "object_storage_bytes": 0},
      "per_slot_incremental_budget": {"engine_active_slots": 1, "device_memory_bytes": 0, "host_memory_bytes": 0, "unified_physical_memory_bytes": 0, "cpu_decode_millicores": 0, "host_pcm_buffer_bytes": 0, "scratch_disk_bytes": 0, "ingress_bandwidth_bps": 0, "egress_bandwidth_bps": 0, "object_storage_bytes": 0},
      "max_active_slots": 1
    }
  ],
  "resource_envelope": {
    "resident_budget": {"engine_active_slots": 0, "device_memory_bytes": 0, "host_memory_bytes": 0, "unified_physical_memory_bytes": 0, "cpu_decode_millicores": 0, "host_pcm_buffer_bytes": 0, "scratch_disk_bytes": 0, "ingress_bandwidth_bps": 0, "egress_bandwidth_bps": 0, "object_storage_bytes": 0},
    "loading_peak_budget": {"engine_active_slots": 0, "device_memory_bytes": 0, "host_memory_bytes": 0, "unified_physical_memory_bytes": 0, "cpu_decode_millicores": 0, "host_pcm_buffer_bytes": 0, "scratch_disk_bytes": 0, "ingress_bandwidth_bps": 0, "egress_bandwidth_bps": 0, "object_storage_bytes": 0},
    "per_slot_incremental_budget": {"engine_active_slots": 1, "device_memory_bytes": 0, "host_memory_bytes": 0, "unified_physical_memory_bytes": 0, "cpu_decode_millicores": 0, "host_pcm_buffer_bytes": 0, "scratch_disk_bytes": 0, "ingress_bandwidth_bps": 0, "egress_bandwidth_bps": 0, "object_storage_bytes": 0},
    "max_active_slots": 1,
    "realtime_reserved_slots": 0
  },
  "rate_and_segment_limits": {
    "max_prefill_tokens_per_sec": 1,
    "max_output_tokens_per_sec": 1,
    "max_segment_audio_ms": 30000,
    "max_render_unit_codepoints": 2000,
    "max_output_audio_ms": 30000,
    "hard_output_quota": true
  },
  "cancellation_and_fencing": {
    "cancellation_mode": "connection_drop_with_quarantine",
    "supports_request_abort": false,
    "supports_fencing_barrier": false,
    "worst_case_non_preemptible_ms": 1,
    "max_drain_wait_ms": 1,
    "idle_reclaim_cooldown_ms": null
  },
  "qualification": {
    "qualification_report_id": "qrep-<...>",
    "qualified_at": "<ISO-8601>",
    "expires_at": "<ISO-8601>",
    "config_bundle_digest": "sha256:<config-bundle-digest>",
    "verified_coexistence_profiles": ["<profile>"]
  }
}
```

模板中的 `0` 只是形状占位，不是合法容量值。真实填写时，未测维度必须明确写“声明上界”并在报告中标注；不能把模板原样提交。

### 6.4 变更矩阵

| 变化 | 是否新 manifest/report | 需要重跑 |
| --- | --- | --- |
| `manifest_id` / `binding_revision_id` / `report_id` 变化 | 是，新 ID | 全部受影响的 cross-doc 校验 |
| 模型 revision、模型文件、模型卡变化 | 是 | 模型摘要、所有质量场景、峰值/加载 |
| 引擎镜像/runtime 变化 | 是 | 引擎组件摘要、质量、取消/并发 |
| 主机拓扑、CPU 架构、内存架构、总物理内存变化 | 是 | 拓扑摘要、容量、共存 |
| `pool_id`、`service_class`、`management_mode` 变化 | 是 | 池/准入/实时 SLO |
| 任一 stage 的三组向量、`max_active_slots`、`realtime_reserved_slots` 变化 | 是 | 主机/实例账本、准入、并发 |
| `max_segment_audio_ms`、速率/输出配额变化 | 是 | 定义一致性、长音频/输出上限 |
| 取消/fencing 能力变化 | 是 | 专项取消/隔离/预算释放验证 |
| 仅修正文档错别字、注释性说明 | 否 | 不改变证据字节；不得改已发布 JSON |
| 报告过期 | 是（重新资格） | 至少全部 required 场景；失效期内不得继续声明 production |

### 6.5 失效与撤回

- 证据到期、模型/镜像/拓扑/池/向量/限额/取消能力任一变化、实测发现容量超限或质量退化，都是失效事件；
- 已发布 publication 是 insert-only，不能原地改证据或回滚：发布更大的 revision，并在新证据里引用旧证据作为历史；
- 旧 manifest/report 和原始输出保留，不覆盖、不重写；失败记录与成功记录分开；
- 失效期间不得用旧 `experimental` 证据支撑 `production`；是否降级/停流由唯一 SM+PO 按产品 Gate 决定。

## 7. 摘要关系（必须按字节/规范化区分）

| 摘要 | 输入 | 算法 | 用途 |
| --- | --- | --- | --- |
| `model_revision_digest` | `artifact-pin.json` 的 `model` 对象 | RFC 8785（JCS）规范化后 SHA-256，前缀 `sha256:` | stage 与模型 pin 绑定 |
| `topology_digest` | `{host_node_id, cpu_arch, hardware_class, memory_architecture, total_physical_memory_bytes}` | RFC 8785 + SHA-256 | manifest/report/host_nodes 绑定 |
| `config_bundle_digest` | `{"artifact_pin": <完整 pin 对象>, "binding_definition": <完整定义对象>}` | RFC 8785 + SHA-256 | 固定“这份定义 + 这份 pin”；manifest 与 instance `config_digest` 引用 |
| `evidence_digest` | `evidence.json` 的**原始文件字节** | SHA-256 | 报告固定整份证据文件 |
| scenario `evidence_digest` | `evidence.json` 中该 scenario 的 JSON 对象 | RFC 8785 + SHA-256 | 逐场景反查，不用信任报告文字 |
| `capacity_manifest_digest` | manifest JSON 的**原始文件字节** | SHA-256 | publication evidence 固定文件内容 |
| `qualification_report_digest` | report JSON 的**原始文件字节** | SHA-256 | publication evidence 固定文件内容 |
| `source.body_sha256` | GitHub comment 的 `body` 原始 UTF-8 字节 | SHA-256 | 追溯转述来源；`body_bytes` 必须同源 |

注意：`config_bundle_digest` 覆盖定义对象，因此定义里新增 `capacity_manifest_id` 会改变它；这是预期的。当前仓库的 `binding-definition.json` 没有该字段，样例使用 `binding-definition.candidate.json` 作为“未来 revision 的定义内容”来计算摘要。真实发布时必须把候选内容作为新 revision 落库，再重算并写入 manifest；不能直接改历史定义，也不能拿候选文件冒充已发布定义。

## 8. 真实样例：GB10 / Qwen3-ASR-1.7B

### 8.1 样例身份

| 项 | 值 |
| --- | --- |
| `manifest_id` | `capm-file-asr-qwen3-1p7b-gb10-20261027` |
| `binding_revision_id` | `mbrev-file-asr-qwen3-1p7b-gb10-20261027`（提议值，尚未在真实 DB 注册） |
| `pool_id` / `service_class` | `asr-batch` / `batch` |
| `management_mode` / `release_tier` | `external` / `experimental` |
| `topology_ref.host_node_profile_id` | `gb10-cube-ccba-unified-121g` |
| `topology_ref.topology_digest` | `sha256:1510168f01f3e4d3bb098b12dfd4ca33ee096239953b3fe8355e6eacb6c71aa4` |
| stage | `stage-qwen3-asr-1p7b-batch`，`asr_batch`，`vllm`，`max_active_slots=1` |
| 模型摘要 | `sha256:886652c5efb861de36ba18d46f58e9f9a7fb2d4469023264c992b055dd6ac1ca` |
| 配置包摘要 | `sha256:86626578d8937c895b27a346a9663dd45215bb9146f282d265f7a0c56de4d52e` |
| 资格期 | `2026-10-27T20:44:19Z` → `2027-01-25T20:44:19Z` |
| manifest 文件 SHA-256 | `sha256:269096a91dd4b4c09e6332bf490889c5e060be67510185c911218042e24ae149` |
| report 文件 SHA-256 | `sha256:e4f3900bd171eb58c9ac91650f64f2e57b556737f3d715893debc8b210c9cd37` |
| evidence 文件 SHA-256 | `sha256:082bd34a404c811d179c982df5f9fab1e6dc39f9e495a1b824da67708549d634` |

### 8.2 资源数值与来源

| 维度 / 限额 | 值 | 来源等级 | 说明 |
| --- | --- | --- | --- |
| `unified_physical_memory_bytes` resident/peak | 12 GiB = 12,884,901,888 B | **声明上界** | #243 的实验 cgroup 上限；不是观测到的峰值。报告限制明确写了“Peak resident memory was not observed” |
| `device_memory_bytes` / `host_memory_bytes` | 0 / 0 | 声明 | 统一内存架构，统一计入 unified，避免双计 |
| `cpu_decode_millicores` resident/peak/per-slot | 2000 / 3000 / 500 | **声明上界** | 没有 CPU 毫核实测；不能写成“实测” |
| `host_pcm_buffer_bytes` resident/peak/per-slot | 2 MiB / 2 MiB / 2 MiB | **声明上界** | 没有 PCM 高水位实测 |
| `scratch_disk_bytes` | 0 | 声明 | 本实验不使用 scratch；不是“测得为 0” |
| `ingress_bandwidth_bps` / `egress_bandwidth_bps` per-slot | 1,000,000 / 256,000 | **声明上界** | 没有带宽实测；host 也没有对应账本列 |
| `max_active_slots` / `realtime_reserved_slots` | 1 / 0 | 声明 | 单实例单 slot；没有并发实测 |
| `max_segment_audio_ms` | 30000 | 定义/产品 | 与 `binding-definition.json` 的 `parameter_domains.asr.max_segment_ms` 一致 |
| `max_prefill_tokens_per_sec` / `max_output_tokens_per_sec` | 1952 / 1952 | 声明 | 来自模型上下文/实验配置；不是吞吐实测 |
| `hard_output_quota` | true | 声明 | 依赖引擎先验限流；本报告没有验证硬配额执行路径 |
| cancellation | `connection_drop_with_quarantine`，abort/fencing=false，worst_case 10000 ms | **未验证** | 没有 abort/fencing 实测；不能声明 `engine_abort_confirmed` |

### 8.3 实测 / 转述 / 未验证

| 类别 | 内容 |
| --- | --- |
| **本任务实际执行** | 读取仓库文件、核对 schema、计算 JSON/文件摘要、运行校验脚本正例与负例。没有 GPU 测量。 |
| **他人实测、本文转述** | #243 comment `6453106316` 的 FLEURS 25 段、279.5 s 长音频、8 kHz 派生、AISHELL-4 远场、ASCEND 混说、babble/重叠/噪声、模型加载 18.4 s、KV cache 2.34 GiB、11.24x 算术上界。原始主记录 `DEL-103-evidence.md` 未提供。 |
| **仓库静态声明** | artifact pin 的模型/文件 digest、Apache-2.0/MIT 许可、镜像 digest、运行时候选版本。模型文件未在本任务下载，未重算权重字节。 |
| **未验证** | 网关 E2E、artifact 持久化、correlation/timeout/retry、取消/abort/隔离/预算释放、并发/P95/持续吞吐、真实街道/餐厅/产品麦克风噪声、方言/口音、真实电话信道、峰值内存、CPU/PCM/scratch/带宽、超过 279.5 s 的上下文行为、生产容量与故障切换。 |

### 8.4 当前样例的诚实结论

- 这是一份**实验级、未完成资格**的证据样例：5 个 required 场景中 2 个通过、3 个 skipped；`overall_status=incomplete`；不能用于 production。
- 12 GiB 是保守 cgroup 上限，不是实测峰值；CPU/PCM/scratch/带宽是声明上界；`resource_pools` 没有数值配额。
- 当前仓库 `binding-definition.json` 的 `execution_recipe` **没有** `capacity_manifest_id`，且 `release_tier=production`；与这份 experimental 证据不一致。真实发布前必须新建 revision，把候选定义中的 `capacity_manifest_id` 和 `release_tier` 落库，重算 `config_bundle_digest`，并重新资格。不能直接改历史定义或把 `local-test-capacity-1` 写入真实路径。

## 9. 校验脚本

### 9.1 命令

在隔离 checkout 中，按本任务的工具链映射执行（不要指向共享库）：

```bash
UV_CACHE_DIR="$PWD/.uv-cache" \
UV_PYTHON=/opt/homebrew/bin/python3.14 \
uv run --no-project \
  --with 'jsonschema>=4.23,<5' \
  --with 'rfc8785>=0.1.4,<0.2' \
  python scripts/dev-env/verify-release-evidence.py \
  --manifest deploy/engines/file-asr-audio/evidence/gb10-20261027/capacity-manifest.json \
  --report deploy/engines/file-asr-audio/evidence/gb10-20261027/qualification-report.json \
  --evidence deploy/engines/file-asr-audio/evidence/gb10-20261027/evidence.json \
  --expect-manifest-id capm-file-asr-qwen3-1p7b-gb10-20261027 \
  --expect-report-id qrep-file-asr-qwen3-1p7b-gb10-20261027 \
  --artifact-pin deploy/engines/file-asr-audio/artifact-pin.json \
  --binding-definition deploy/engines/file-asr-audio/evidence/gb10-20261027/binding-definition.candidate.json \
  --database-snapshot tests/fixtures/release-evidence/gb10-20261027-db-snapshot.json \
  --source-body /path/to/comment-body.md \
  --require-published --require-digests --show-digests
```

`--database-url` 与 `--database-snapshot` 二选一；live 模式只读事务（`SET TRANSACTION READ ONLY`），不会写 publication 或证据。`--source-body` 可选；不提供时 `digest.source_body=NA`，只核对记录值。

退出码：`0` 表示无 `FAIL`（可能有 `WARN/NA`）；`1` 表示至少一个 `FAIL` 或输入错误；`2` 表示缺依赖/参数错误。`PASS_WITH_WARNINGS` 不是生产放行；fixture、`NA`、未验证项必须逐条解释。

### 9.2 它检查什么

- 两份冻结 schema；
- 可选的 `--expect-manifest-id` / `--expect-report-id`：断言你拿到的发布参数与文件内 ID 相同；
- 包络聚合；
- 报告 scenario id 一一对应、required/overall 计算、production gate、limitations；
- manifest/report/evidence 的 id、tier、topology、时间、stage/engine 组件；
- `artifact-pin.model` 摘要、`config_bundle_digest`、`topology_digest`、evidence 文件/逐 scenario 摘要、source body 字节；
- report `evidence_digest` 与 manifest/report 文件 SHA；
- 传入的 `--binding-definition` 与快照内定义分别检查 `capacity_manifest_id`、可识别的 `release_tier`、stage roles、tasks、max segment；
- publication/evidence 行的身份与两个文件摘要；
- instance 三组六维向量、slot、状态、config digest；
- host 拓扑、allocatable/held/quarantine、peak 覆盖、剩余 slot 余量；
- pool 状态/服务等级；workspace attempt 配额；
- `local-test-*` 占位 ID 拒绝。

### 9.3 最小 SQL 对照

脚本不是唯一手段；下面这组只读查询表达同样的“三者一致”核心。表名按当前产品 schema `speech`；`<...>` 换成实际值。

```sql
-- 0) 直接按发布参数里的两个 id 找到证据行
SELECT kind, object_id, revision, revision_id,
       capacity_manifest_id, qualification_report_id,
       capacity_manifest_digest, qualification_report_digest
FROM speech.speech_binding_publication_evidence
WHERE kind = 'binding'
  AND capacity_manifest_id = '<manifest-id>'
  AND qualification_report_id = '<report-id>';

-- 1) 定义声明的 manifest id
SELECT r.binding_id, r.revision, r.revision_id,
       r.definition_json::jsonb #>> '{execution_recipe,capacity_manifest_id}' AS declared_manifest_id
FROM speech.speech_binding_revisions r
WHERE r.binding_id = '<binding-id>' AND r.revision = '<revision>';

-- 2) publication + evidence 固定的事实
SELECT p.kind, p.object_id, p.revision, p.revision_id, p.published_at_us,
       e.capacity_manifest_id, e.qualification_report_id,
       e.capacity_manifest_digest, e.qualification_report_digest
FROM speech.speech_publications p
LEFT JOIN speech.speech_binding_publication_evidence e
  ON e.kind = p.kind AND e.object_id = p.object_id AND e.revision = p.revision
WHERE p.kind = 'binding' AND p.object_id = '<binding-id>' AND p.revision = '<revision>';

-- 3) 实例声明的容量向量与 slot
SELECT instance_id, instance_generation, host_node_id, pool_id,
       binding_revision_id, manifest_id, management_mode, loading_policy,
       config_digest, instance_state, base_budget_kind, stopped_at_us,
       max_slots, realtime_reserved_slots, held_slots, quarantined_slots,
       peak_unified_physical_memory_bytes, steady_unified_physical_memory_bytes,
       per_slot_unified_physical_memory_bytes
FROM speech.engine_instances
WHERE binding_revision_id = '<revision-id>';

-- 4) 主机预算与持有/隔离
SELECT host_node_id, memory_architecture, node_state, topology_digest,
       allocatable_unified_physical_memory_bytes,
       held_unified_physical_memory_bytes,
       quarantined_unified_physical_memory_bytes
FROM speech.host_nodes WHERE host_node_id = '<host-node-id>';

-- 5) 池与服务等级（没有数值 quota 列）
SELECT pool_id, service_class, pool_state
FROM speech.resource_pools WHERE pool_id = '<pool-id>';

-- 6) workspace attempt 配额
SELECT workspace_id, max_attempts, held_attempts, quarantined_attempts
FROM speech.workspace_quotas WHERE workspace_id = '<workspace-id>';
```

判定：定义 `declared_manifest_id` = 参数/证据 `capacity_manifest_id`；publication `revision_id` = 定义 `revision_id` = 实例 `binding_revision_id`；实例六维 = manifest 三组向量；host `held >= peak` 且 `held + quarantined <= allocatable`；池 active/class 匹配；workspace `held + quarantined <= max_attempts`。

## 10. 已知缺口与后续卡建议

1. **`resource_pools` 没有数值配额列**：当前只能核验池身份/状态/服务等级，不能核验池总量。建议单独卡增加池级 quota 与准入扣减，不要在本任务改 schema/产品代码。
2. **当前 `binding-definition.json` 缺 `capacity_manifest_id` 且声明 `production`**：本任务用 `binding-definition.candidate.json` 展示未来 revision 的声明，不修改历史定义。建议由绑定作者新建 revision；发布前重算 `config_bundle_digest`。
3. **许可不是 schema 字段**：建议在发布检查清单或独立证据表中记录许可证接受；不要在 manifest 中新增自由字段。
4. **阈值不可机器读**：报告只记录 `status` 与 `notes`，没有 `threshold` 字段；建议后续 schema 演进时增加机器可读阈值，本任务不改变冻结契约。
5. **stage graph 未表达 VAD/对齐/分离**：如果实际执行路径包含这些阶段，必须新增 stage 并重新资格；不能仅靠 `parameter_defaults`。
6. **产品发布校验只检查 capacity manifest id**：`publications_postgres.py` 当前不核对 release tier、topology、摘要、host/instance 账本；脚本是补强，不是产品门禁。建议后续卡把关键检查接入发布路径。
7. **带宽/对象存储没有 host 账本列**：manifest 有声明，但主机层无法按列扣减；需要平台监控/后续 schema 演进。
8. **fixture 不是发布回执**：`tests/fixtures/release-evidence/gb10-20261027-db-snapshot.json` 只证明查询路径；真实发布必须用 live DB 的 publication/evidence 行重新跑 `--database-url`。

## 11. 文件清单（本任务新增/修改）

| 文件 | 作用 |
| --- | --- |
| `docs/operations/capacity-manifest.md` | 本文 |
| `docs/operations/qualification-report.md` | 配套报告定义 |
| `deploy/engines/file-asr-audio/evidence/gb10-20261027/capacity-manifest.json` | 真实数值样例 |
| `deploy/engines/file-asr-audio/evidence/gb10-20261027/qualification-report.json` | 配对报告 |
| `deploy/engines/file-asr-audio/evidence/gb10-20261027/evidence.json` | #243 结构化转述 |
| `deploy/engines/file-asr-audio/evidence/gb10-20261027/binding-definition.candidate.json` | 未来 revision 的候选定义（非部署） |
| `deploy/engines/file-asr-audio/evidence/gb10-20261027/README.md` | 样例包说明与摘要 |
| `tests/fixtures/release-evidence/gb10-20261027-db-snapshot.json` | 查询路径 fixture |
| `scripts/dev-env/verify-release-evidence.py` | 只读一致性校验 |
| `docs/operations/release-evidence-verification-output.txt` | 实际运行原始输出 |
