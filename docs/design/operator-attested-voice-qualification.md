---
title: 外部托管引擎的运维自证声音资格
status: active
version: "1.0.0"
date: 2026-11-16
---

# 外部托管引擎的运维自证声音资格

本文定义内建声音资格的**第二条来源**：由部署方主动声明信任、以运维自证（`operator_attested`）
替代 node-controller 观测（`controller_observed`），让「引擎外部托管、部署方无权重启」的部署也能取得
内建声音资格。它是与 controller 观测**并列**的一条受限路径，不是把 controller 观测改成可选。

适用声音仍固定为 `Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice` / `Vivian`。本路径只解决**声音资格**；
它不产生任何运行态事实（native evidence、停止证据、资源释放、实例生命周期收敛），因此
不打开依赖 controller 观测的能力边界（§5）。

## 1. 问题与既有事实

参考部署（`deploy/compose.yaml`）按设计不含 node-controller：「engine lifecycle is
operator/external in this bootstrap」。在该部署下，同步 TTS 拿不到声音资格，三条事实同时成立：

| 事实 | 出处 |
| --- | --- |
| 资格与探针授权**强制**要求当前 READY 观测携带 `native_runtime_evidence` | `speech/domain/qualification_native_observation.py`（`require_native_ready`：`native is None` → `native_runtime_evidence_unavailable`） |
| 手动注册（`_spec_from_manual`）**结构上不产生**它：合成观测没有 native 证据，也没有可投递的 controller intent | `engine_registration.py`（`runtime_intent_deliveries` → `delivery_phase='blocked'`, `last_failure_code='operator_attested'`） |
| 无 controller 时，`speech_builtin_voice_approvals` 没有任何合法生产者 | `qualification_probe_approval.py`（只接受 probe 执行终态证据） |

后果：`has_qualified_voice` 为假，`/v1/audio/speech` 恒 `503 backend_unavailable`
（`no current qualified voice`）。2026-11-03 的一次真实 `200` 是在受控窗口里由 controller 托管引擎
跑出来的，能力存在但本部署无法复现。

手动注册本身**可以**提供 READY 观测与 readiness 回执（`engine_registration.py` 的
`_register_in_transaction` 合成 observation 与 `VerifiedRuntimeReadiness`），所以 TTS
准入链的其余部分（实例头、readiness、租约、结算）在外部托管引擎上已经可用。
缺的**只有**声音资格这一项——这也是本设计只处理资格、不触碰运行态的原因。

## 2. 信任模型

### 2.1 两条来源，两种信任根

| | controller 观测（既有） | 运维自证（本设计新增） |
| --- | --- | --- |
| 信任根 | node-controller 产出的 READY 观测（native 镜像/模板/控制器摘要 + native auth） | 部署方事先签署的**信任声明**（`trust declaration`）+ 运维对账与发布 |
| 资格证据 | 受控 probe 的真实执行：完整 WAV、`engine_terminal_response`、停止与释放双证据 | 运维声明的制品摘要、实例身份、fencing、时效窗口；由 CLI 现场重算并与持久账本交叉核对 |
| 写入者 | `speechnexus-run-builtin-qualification`（`--issue/--execute/--approval-id`） | `speechnexus-attest-builtin-voice`（新） |
| 事实记录 | `speech_builtin_voice_approvals`（probe 关联）+ `speech_builtin_voice_qualifications` | 同两张表（`operator_attested` facts 变体）+ `speech_builtin_voice_operator_attestations` |
| 来源标记 | `speech_builtin_voice_qualification_provenance.source_kind='controller_observed'` | 同表 `source_kind='operator_attested'` + `attestation_id` |

两条来源产出的资格在下游（准入、目录、撤销）**同构**：都写入同一份
`BuiltinVoiceFacts`、同一条 `speech_builtin_voice_qualifications` 行、同一条 publication。
来源差异只存在于持久记录（provenance 行 + attestation 行）与 facts 的 verifier 变体，
因此下游读取路径不需要为来源分支，也无法把一种来源误读成另一种。

### 2.2 默认关闭、fail-closed

1. **没有任何自动降级**：`require_native_ready`、`issue_probe_authorization`、
   `PostgresQualificationAdmission`、`_commit_verified_probe` 全部保持原样；观测缺 native 证据时
   仍然拒绝，不会改走运维自证。
2. **信任声明是写入前提**：`speechnexus-attest-builtin-voice` 必须收到
   `--trust-declaration-file`，且该文件必须通过全部结构校验（格式、目标 installation、
   被信实例列表、必须显式承认的能力缺口）。缺失或不符 → 命令以命名错误码拒绝，**零写入**。
3. **不声明信任就没有资格**：本路径之外不存在任何写 `speech_builtin_voice_operator_attestations`
   的代码路径；没有 attestation 行就无法构造 `operator_attested` approval，
   发布入口也会拒绝 `operator_attested` provenance（`not_qualified`）。
4. **写入身份受限**：`SPEECHNEXUS_OPERATOR_ATTESTATION_WRITER_DATABASE_URL` 指向
   `builtin_operator_attestation_writer_grants` 授权的最小身份；命令在事务内检查
   该身份不能是 owner/superuser、不能写历史账本（相当于 `writer_identity_forbidden` 的同构检查）。

### 2.3 时效

运维自证带显式时效窗口 `[observed_at, expires_at]`，同时受四个上界约束：

1. 声明的 `max_attestation_validity_seconds`（信任声明内，≤ 30 天）；
2. 来源批准 `speech_builtin_voice_source_approvals.expires_at`；
3. binding release 证据的有效期（`verify_binding_release_evidence().expires_at`）；
4. `now + max_attestation_validity_seconds`。

`expires_at` 写入 `speech_builtin_voice_approvals.expires_at`，因此既有读路径（
`require_builtin_voice` 要求 `a.expires_at > clock_timestamp()`）自动执行过期拒绝——
运维自证不会因为「写进持久记录」而获得无限期。窗口外再发布/再发布后过期均由既有读路径拒绝。

## 3. 字段与出处

### 3.1 信任声明（`speechnexus-operator-attested-engine-v1`）

运维提供的私有 JSON（≤ 64 KiB，绝对路径，仓库外），按键集合精确校验：

| 字段 | 含义 | 校验 |
| --- | --- | --- |
| `format` | 固定 `speechnexus-operator-attested-engine-v1` | 字面量 |
| `declared_by` | 作出信任决定的人/职责标识 | `Identifier`，与 attestation 的 `declared_by` 一致 |
| `declared_at` | 决定时间（带时区，微秒精度） | 不得晚于当前时间 |
| `installation` | `{database, schema, installation_id}` | 必须等于事务内读到的目标安装（`speech_publication_installation`） |
| `scope.lifecycle_controller` | 固定 `operator_external` | 字面量；表示引擎生命周期由运维掌握 |
| `trusted_instances` | 允许被自证的实例 ID 列表 | 命令的 `instance_id` 必须在内 |
| `acknowledged_gaps` | 必须显式承认的缺口集合 | 必须包含 §5 列出的全部固定缺口码 |
| `max_attestation_validity_seconds` | 单次自证时长上限 | 整数，`0 < v <= 2_592_000` |

`acknowledged_gaps` 的固定集合：

```
no_controller_observation        # 没有 node-controller READY 观测
no_native_runtime_evidence       # 没有 native 镜像/模板/控制器摘要证据
no_controller_stop_evidence      # 没有 controller 侧停止/释放证据
no_budget_release_by_attestation # 自证不结算算力释放
no_lifecycle_convergence_by_attestation  # 自证不收敛实例生命周期
```

声明原文与规范化摘要写入 attestation 行，读面可复核；声明生效前提是运维真的把这段文字
当作部署决策签署，而不是命令自动生成。

### 3.2 自证请求（`speechnexus-operator-attested-qualification-v1`）

| 字段 | 出处 | 校验（独立复核方式） |
| --- | --- | --- |
| `attestation_id` / `approval_id` | 运维选择 | `Identifier`；重放按两者精确比对 |
| `installation` | 部署目标 | 与 DB 当前目标逐字段相等 |
| `voice.{voice_id,revision,revision_id,source_approval_id}` | 声音目录 + 来源批准 | 来源批准必须存在、未撤销、未过期、`native_speaker_id='Vivian'` |
| `binding.{binding_id,binding_revision_id}` | 私有 recipe / binding 发布记录 | `speech_binding_revisions` 存在该 revision、未被禁用；publication 与 evidence 存在 |
| `engine.{instance_id,instance_generation,fencing_token}` | 手动注册结果 | `engine_instances`（该 generation）与 `runtime_instance_heads.current_generation` 相等；`resource_pools` 当前 owner `fencing_token` 相等且未过期；`engine_instances.last_fencing_token` 相等；`instance_state='ready'` |
| `engine.template_id` | 节点模板 | 等于注册的 `approved_template_id` |
| `engine.image_digest` | 节点模板镜像 | 等于 release 证据中该 stage pin 的 `image_digest`（独立来源） |
| `engine.template_document` | 运维模板原件（`speechnexus-template-v1`） | 现场重算规范化摘要 = `template_digest`；`template.template_id`/`image_digest`/`engine_auth` 与注册、release 证据相符 |
| `engine.lifecycle_document` | 运维生命周期原件（`speechnexus-external-lifecycle-v1`） | 现场重算规范化摘要 = `declared_lifecycle_digest`（落库名；与 `require_native_ready` 的 `controller_artifact_digest` 在语义位置上对应，但前缀显式标明它是**声明**的快照）；文档内 `instance_id/instance_generation/fencing_token/image_digest/template_id/declared_by` 必须与其余输入相等 |
| `model.{model_id,model_revision,model_artifact_digest}` | artifact pin + release 证据 | `model_artifact_digest` 必须命中 release 证据同一 stage pin 的 `model_digest`；`model_id/revision` 等于 artifact pin 的 `model` |
| `capabilities` | 运维声明 | 逐项 ⊆ binding 定义 `parameter_domains.tts`；`max_input_codepoints` ≤ 域上限 |
| `window.{observed_at,expires_at}` | 运维声明 | 带时区；`observed_at <= now < expires_at`；`expires_at-observed_at <= 声明上限`；`expires_at` ≤ 来源批准/ release 证据有效期 |

`gateway_policy_revision_id`、`gateway_policy_digest`、`gateway_id`、`host_node_id` 不来自运维键入，
而是取自持久账本与 gateway live policy 证据（§3.3），因此不能靠改请求文件伪造。

### 3.3 交叉核对的三类独立事实

1. **持久注册账本**：`engine_instances`、`runtime_instance_heads`、`resource_pools`、
   `workspace_quotas`（`policy_revision`）、`speech_binding_revisions`、
   `speech_binding_publication_evidence`、`speech_publications`。
2. **release 证据**：`verify_binding_release_evidence`（既有只读组件）对 manifest/report/
   scenario/artifact pin 原字节复核；只证明内容一致，不证明物理执行。
3. **gateway live policy**：复用 `require_live_gateway_policy`，要求认证后读取的 live
   TTS policy 与声明逐字段相等（policy 摘要、voice 映射、instance/generation、node）。
   gateway 是独立进程，不是运维键入的字段。

### 3.4 facts 变体（机器可分）

`BuiltinVoiceFacts` 增加第二种合法变体，键集合与 controller 变体**互斥**：

| | controller 变体 | operator 变体 |
| --- | --- | --- |
| `verifier_id` / `verifier_version` | `builtin-customvoice-v1` / `1` | `operator-attested-customvoice-v1` / `1` |
| 证据摘要字段 | `probe_evidence_digest`（探针执行证据） | `declaration_digest`（信任声明+制品+声明的规范化摘要）；**不含** `probe_evidence_digest` |
| 额外对象 | — | `attestation: {kind, attestation_id, declared_by, declaration_digest, template_digest, lifecycle_digest, image_digest, model_artifact_digest, instance_id, instance_generation, fencing_token, observed_at_us, expires_at_us}` |
| `required_checks` | `source_approved, artifact_integrity, engine_identity, voice_speaker_binding, wav_complete, output_bounds, native_terminal, stop_release` | `source_approved, artifact_integrity, engine_identity, voice_speaker_binding, declared_capabilities, instance_registration, attestation_window, gateway_policy` |

operator 变体在 `BuiltinVoiceFacts` 内自洽校验：`attestation` 与顶层
`image_digest/model_artifact_digest/instance_id/generation/checked_at_us/expires_at_us/declaration_digest`
逐项相等。因此仅凭一行 facts 就能看出「这不是 controller 观测来的」，无需额外 join。
controller 变体的校验规则一字不放宽。

### 3.5 持久结构（迁移 `0055_operator_attestation`）

```text
speech_builtin_voice_operator_attestations      -- 一次运维自证的完整声明
  attestation_id PK, approval_id UNIQUE FK-> approvals
  voice_id/revision/revision_id/binding_revision_id/source_approval_id
  instance_id, instance_generation, fencing_token, host_node_id, gateway_id, pool_id
  policy_revision_id, gateway_policy_digest
  template_id, image_digest, template_digest, declared_lifecycle_digest
  model_id, model_revision, model_artifact_digest
  observed_at_us, expires_at_us
  declaration JSONB, declaration_digest
  attestation_intent JSONB           -- 原请求原文，供重放比对
  declared_by NAME, declared_at TIMESTAMPTZ

speech_builtin_voice_qualification_provenance   -- 每条已发布内建资格的来源
  (voice_id, revision) PK FK-> speech_builtin_voice_qualifications
  source_kind CHECK IN ('controller_observed','operator_attested','unverified')
  evidence_kind CHECK IN ('probe_authorization','operator_attestation','absent')
  attestation_id FK NULL            -- 仅 operator_attested
  probe_authorization_id FK NULL    -- 仅 controller_observed
  CHECK 三态一一对应（见下）
  recorded_by, recorded_at
```

- **不回填、不改写历史**：迁移只在 `speech_builtin_voice_qualifications` /
  `speech_builtin_voice_approvals` 之外新增两张表，不改任何既有列与既有行。
  **来源标签由证据支撑，不由缺席推出**。`controller_observed` 必须同时给出
  `probe_authorization_id`：该批准列由 probe 路径写入，而声明身份没有该列的
  `INSERT` 权限，所以它伪造不出来。带 operator facts 的批准必须给出与自身逐项一致的
  attestation 且不得携带 probe 授权；两者都不成立时记 `source_kind='unverified'`、
  `evidence_kind='absent'`，读面显示为「来源未知」，而不是悄悄算作控制器观测。
  0055 之前发布的资格没有 provenance 行：读面按批准自身是否带 `probe_authorization_id`
  归为 `controller_observed`（`evidence=legacy_pre_0055`）或 `unverified`，这是读面对历史
  的记录，不是准入结论——准入要求 provenance 行本身，缺它一律拒绝（见 §3.6）。
- **`declared_*` 命名**：`declared_lifecycle_digest`／读面的 `declared_fencing_token`、
  `declared_instance_generation`、`declared_observed_at_us`、`declared_expires_at_us`
  都是声明的快照，不是当前运行态；读面注释与字段前缀同时表达这一点。
- **downgrade**：无 attestation 行时 `DROP` 两张新表即可（不改既有表）；
  已有 attestation 行时拒绝降级（`RuntimeError`，与 0029/0030 的历史保全策略一致），
  避免把 operator 证据降级成「看起来是 controller 观测」的孤儿版本。

### 3.6 准入侧读取来源证据

「来源可读」不等于「来源可信」。写进读面而不进准入，等于把信任决定留在审计报告里：
持凭据者可以绕过信任声明、release 证据与窗口直接合成一条可用的内建资格，读面诚实地写
「来源未知」，而同步 TTS 与公开 Job 照常使用。所以准入侧与读面共用同一个判据。

判据只有一份，位于 `speech/infrastructure/qualification_source_evidence.py`：
`qualification_source_state_sql` 给一行资格判出所处状态（`evidenced` / `absent` /
`unverified` / `controller_without_probe` / `operator_without_attestation` /
`operator_elapsed`），`qualification_source_evidenced_sql` 只是「状态等于 `evidenced`」。
三处读者共用这两个表达式，不允许各自重写：

| 读者 | 用途 |
| --- | --- |
| `require_builtin_voice`（`builtin_voice_qualification.py`） | 同步 TTS 解析与准入事务 |
| `public_job_admission._custom_voice` | 公开 Job 的内建兜底判定 |
| `QualifiedPostgresSpeechFacts.has_qualified_voice` | 启动门：候选查询本身带同一判据，再逐行走 `require_builtin_voice`，两处不会各说各话 |

接受与拒绝只有三种情形，**准入一律要求正向证据**：

| 情形 | provenance 行 | 准入 |
| --- | --- | --- |
| 控制器观测 | `controller_observed` 且 `probe_authorization_id` 非空 | 接受 |
| 运维自证（窗口内） | `operator_attested` 且 attestation 在 `[observed_at_us, expires_at_us)` | 接受 |
| 其余全部 | `unverified`、声明已过窗口、控制器缺探针授权、声明缺 attestation、**没有 provenance 行** | 拒绝，`qualification_source_unverified` |

「没有 provenance 行」不构成历史豁免：provenance 是资格行的子表，数据库层没有「资格行必须有
provenance 行」的约束，而写资格行是发布身份的必需权限。因此手写资格行、或删掉 provenance 行、
或走不通发布命令的 `DELETE`，都能得到「已发布但无 provenance 行」这种形状——它只说明这一行不是
发布命令写的。真历史行同样要先**发布一个新的 voice revision**、再走控制器探测路径或运维自证路径
取得该 revision 的资格，才能重新被准入（同一 revision 不能补发，重复发布会以 `publication_conflict`
拒绝）；
审计读面仍按事实标 `legacy_pre_0055`，那是读面的记录，不是准入许可。发布说明必须写明这条限制
（见 §9 与运维手册的发布说明）。

拒绝码与文案：内部 `qualification_source_unverified` 进入
`ADMISSION_REJECTION_CODES`，按既有 `PUBLIC_ADMISSION_CODES` 映射为公开
`engine_resource_disabled`（部署侧待修、不可重试），不新增公开码、不改契约。**公开文案按状态点名**：
`source_refusal_detail(state)` 给出具体修复动作（都以「发布新的 voice revision 并在其上取得来源」
为落点：无 provenance 行、声明已过期、缺探针授权、缺 attestation 各自点名），不沿用「资源被禁用」
的默认文案，否则运维会去翻开关而不是查证据链（与 #877/#880 的点名规则一致）。

| 消费方 | 线上答案 | 名字落在哪里 |
| --- | --- | --- |
| 公开 Job 内建兜底 | `422 voice_not_published`（该 revision 确实不可用、确定性、不可重试） | 响应 `message` 按状态点名修复动作 |
| 同步 TTS | `404 voice_not_found`（与已撤销资格的既有行为一致，不是 503） | 日志行 `builtin voice qualification refused: voice=… revision=… code=qualification_source_unverified state=…` |

两处都必须保留可区分的名字：同步 TTS 的两种起因（已撤销 / 无证据）在线上是同一个 404，只有日志能分开；
公开 Job 的 `voice_not_published` 默认文案说的是「未发布」，而这件事是「已发布但无证据」。

## 4. 两种来源的边界对比

| 维度 | controller_observed | operator_attested |
| --- | --- | --- |
| 生产者 | `speechnexus-run-builtin-qualification`（probe 执行 + 终态证据） | `speechnexus-attest-builtin-voice`（声明 + 交叉核对） |
| 上线前提 | node-controller 托管实例 + gateway | 部署方信任声明 + 手动注册的 ready 实例 + gateway |
| 是否要求 native evidence | 是（`require_native_ready`，一字不放宽） | 否；改由声明制品摘要 + 注册账本 + live gateway policy 交叉核对 |
| 能力声明来源 | 实测 probe 覆盖范围（en/24000/wav/complete/探针文本长度） | 运维声明，且必须 ⊆ binding 参数域 |
| 时效 | 来源批准 / release 证据 / `now+1d` 三者最小 | 来源批准 / release 证据 / 声明上限 / `now+上限` 四者最小 |
| 停止/释放证据 | 必需（`engine_terminal_response` + `resources_released`） | **不产生、不替代** |
| 资格撤销 | 按 approval_id | 同一张表、同一命令（`--kind qualification`），行为一致 |
| 首次请求是否能拿到 200 | 需要 GPU + controller 窗口 | 需要外部引擎真实可服务（本卡不取证） |
| 读面 | provenance `controller_observed` | provenance `operator_attested` + attestation 明细 |

## 5. 不因运维自证而开放的能力（逐条）

| 能力 | 依据 | 为什么自证不能开口子 |
| --- | --- | --- |
| 资格 probe 授权（`speech_builtin_voice_probe_authorizations`） | `require_native_ready` 要求 `native_runtime_evidence` | 探针授权是「controller 观测成立」的派生事实；自证只写资格账本，不写观测，`require_native_ready` 仍拒绝（`native_runtime_evidence_unavailable`）。 |
| 探针准入与结算（`PostgresQualificationAdmission`） | 同一函数 + 一次性授权 | 自证不签发 authorization / admission 回执，端口的二次核对不变。 |
| 算力释放 / 基础预算归还 | `decide_runtime_observation` 的 stop 分支要求 `stop_evidence`；`RuntimeObservedStop` 构造即要求 `process_or_container_exited` 与 `resources_released_confirmed` | 预算归还只由 controller 观测到的物理停止驱动。自证没有任何字段进入该决策。 |
| `execution_cleanup_pending` 的结算 | `speech/domain/lease_release.py::release` 要求 `cleanup_state='complete'` 且 stop 事实 kind ∈ {`engine_terminal_response`, `instance_terminated_by_controller`, `operator_verified_instance_terminated`} | 这三个 kind 都是执行网关侧的停证据，属于 `submit_intents`/`execution_stop_evidence`，不是资格账本；自证记录无法构造它们。 |
| 实例生命周期收敛（`prepare`/`stop` intent 的投递与观察） | `runtime_intent_deliveries` 只允许 controller 投递；手动注册把投递固定为 `blocked/operator_attested` | 自证不改投递状态、不改 `runtime_instance_intents`、不写 `observation_document`。 |
| readiness 回执续签（public Job 的 `engine_not_ready` 门） | `engine_registration.py::_renew_readiness` 需要真实健康探测 | 自证不写 `readiness_document`；续签仍走 `speechnexus-register-engine --renew-readiness`。 |
| 新 generation 注册 | `previous_generation_active`：上一代必须 `instance_state='stopped'` 且 `base_budget_kind='released'` | 只有 controller 观测过的停止才能把预算标成 released；自证无从推进代次。 |
| 资格范围扩大 | `BuiltinVoiceFacts` + `require_qualified_parameters` | 自证只能声明 ⊆ binding 参数域的能力；越界即拒绝，且不写任何行。 |

结构性证据是**授权面**，不是运行态快照。声明身份 `sn_operator_attestation` 由
`operator_attestation_writer_grants` 声明：对运行态实体 `host_nodes`、`admission_leases`、
`execution_stop_evidence`、`runtime_instance_intents` **连 `SELECT` 都没有**，对
`resource_pools` 与 `workspace_quotas` 只有 `SELECT`；写权限只有
`speech_builtin_voice_approvals` 的八列 `INSERT` 与
`speech_builtin_voice_operator_attestations` 的表级 `INSERT`，没有任何
`UPDATE`/`DELETE`/`TRUNCATE`，也没有资格、发布或 publication head 的写权限。
因此即使这条写入路径被完全控制，它也造不出 native 运行证据、租约、停止证据或预算释放：
能力缺口由权限边界保证，而不是由「这一次观察到它没写」保证。表级能力对照见 §5 表格，
授权函数的实际面由 `tests/integration/test_operator_attested_qualification_pg.py` 逐表断言
（对上述四个运行态实体 `SELECT` 直接 `InsufficientPrivilege`），发布侧的零改动另有前后快照
比对作为补充证据。

## 6. 失败面与错误码

写入命令（`speechnexus-attest-builtin-voice`）对已分类拒绝返回退出码 `4`，
存储不可用/结果未知返回 `5`，参数或配置错误返回 `2`（与既有资格 CLI 一致）。

| 错误码 | 触发 | 是否点名缺失项 |
| --- | --- | --- |
| `trust_declaration_missing` | 未提供信任声明文件，或声明不是绝对路径/超限 | 是（缺哪个输入） |
| `trust_declaration_format_invalid` | `format`/`scope.lifecycle_controller` 不符 | 是 |
| `trust_declaration_target_mismatch` | 声明的 database/schema/installation 与当前目标不符 | 是（逐字段） |
| `trust_declaration_instance_not_trusted` | `instance_id` 不在 `trusted_instances` | 是 |
| `trust_declaration_gaps_unacknowledged` | `acknowledged_gaps` 缺固定缺口码 | 是（列出缺的码） |
| `attestation_request_invalid` | 请求文档结构/值域不合法 | 是（字段名） |
| `template_digest_mismatch` | 模板原件规范化摘要 ≠ 请求声明 | 是 |
| `lifecycle_digest_mismatch` | 生命周期原件摘要 ≠ 声明 | 是 |
| `image_digest_mismatch` | 镜像摘要 ≠ release 证据 stage pin | 是 |
| `model_artifact_digest_mismatch` | 模型制品摘要 ≠ release 证据 stage pin / artifact pin | 是 |
| `instance_not_registered` / `instance_generation_mismatch` / `stale_fencing_token` | 注册账本不符 | 是 |
| `template_identity_mismatch` | `template_id` 与注册的 `approved_template_id` 不符 | 是 |
| `policy_revision_mismatch` | 事务内读不到可用的 `workspace_quotas.policy_revision` | 是 |
| `attestation_window_invalid` / `attestation_window_expired` / `attestation_window_too_long` | 时效窗口非法 | 是 |
| `capability_outside_binding_domains` | 声明能力超出 binding 参数域 | 是（哪个能力项） |
| `source_approval_unavailable` | 来源批准不存在/撤销/过期 | 是 |
| `binding_revision_unavailable` / `binding_release_evidence_mismatch` | binding 或 release 证据不可用 | 是 |
| `gateway_policy_evidence_unavailable` | live gateway policy 不符 | 是 |
| `writer_identity_forbidden` | 写入身份拥有超范围权限（含 publication head 写权限） | 是 |
| `attestation_conflict` | 同一 attestation/approval 身份已用不同原件记录 | 是 |
| `attestation_revision_already_attested` | 该 voice revision 已有一条声明；一条 revision 只承载一条声明，续期须发新 revision | 是 |
| `attestation_missing` / `attestation_mismatch` | 发布时声明与 approval 不符，或探针 approval 被塞入声明记录 | 是 |
| `trust_declaration_declared_by_too_long` | `declared_by` 超过 `NAME` 列的 63 字节（否则会被静默截断成另一个名字） | 是 |
| `qualification_source_unverified`（准入） | 资格行仍在，但其来源没有证据支撑，或运维声明窗口已过 | — （准入拒绝，非写入） |

读面（`speechnexus-qualification-provenance`）只读，不写、不探测引擎。它逐行输出
`source_kind`/`source_evidence`/`provenance_recorded`/`probe_authorization_id`/
`attestation_id`/`declared_by`/窗口/摘要，`operator_attested` 且窗口已过或缺失的行给出
`attestation_state` 与「发新 revision 才能续期」的提示。它只要求
`qualification_reader_grants` 声明的 SELECT 面（qualifications、approvals、provenance、
attestations、probe authorizations），以 `sn_qualification_reader` 身份运行，不写、不探测引擎。
读失败退出码 `3`，从不把「没读出来」当成资格结论。

读面语义：

- `source_kind='controller_observed'`：必须带 `probe_authorization_id`，且
  `provenance_recorded=true` 时 provenance 行记录的 probe 授权必须与 approval 自带的相同；
  0055 之前的历史行按 approval 自身是否带 probe 授权归入此标签（`evidence=legacy_pre_0055`）。
- `source_kind='operator_attested'`：运维自证，附 attestation 明细，运维能看出「这不是
  controller 观测来的」。
- `source_kind='unverified'`（`source_evidence='absent'`）：既没有 probe 授权也没有声明记录，
  显式标为来源未知而不是算作 controller 观测；这类行在准入侧被拒绝（见 §3.6）。
- `evidence=legacy_pre_0055`：0055 之前没有 provenance 行的历史行。读面照实标出这段历史；
  准入不因标签放行，这类行同样要发布新的 voice revision 才能被接受。
- 只有 `controller_observed` 行输出 `probe_authorization_id`；operator 行不输出该字段，
  避免「有授权但为空」的误读。

### 6.1 续期与一次性语义

一条 voice revision 只承载一条声明：`(voice_id, revision, revision_id,
binding_revision_id)` 上的唯一约束是反替代规则——否则第二条声明会被读成第一条的替换。
声明到期不释放该 revision，再次声明同一 revision 以
`attestation_revision_already_attested` 拒绝（退出码 `4`，不是「结果未知」）。续期流程是
发布新的 voice revision（新 `revision` 与 `revision_id`）并对其声明，旧 revision 的资格与
provenance 行保留为历史事实。

## 7. 迁移方案

- 新增迁移 `0055_operator_attestation`（`down_revision='0054_artifact_deleted_ledger'`）：
  只 `CREATE TABLE` 两张新表，不 `ALTER` 既有表，不触碰既有行。
- `downgrade()`：先锁表检查 `speech_builtin_voice_operator_attestations` 是否为空；
  空则 `DROP TABLE provenance` → `DROP TABLE attestations`；非空则 `RuntimeError`
  拒绝（历史保全）。
- 授予：新增 `operator_attestation_writer_grants`（最小列级 INSERT；
  只能写 approval 与 attestation，不能写 qualifications/publications，也不持有任何
  `UPDATE`/`DELETE`）；`builtin_publisher_grants` 增加读 attestation、写 provenance 的权限。
- 已发布资格的 `speech_builtin_voice_qualifications`、`approvals` 行与 rollback 后仍保持一致，
  因为新表全是附加事实。

## 8. 验收映射

| 验收 | 用例 |
| --- | --- |
| 正例 | `tests/integration/test_operator_attested_qualification_pg.py`：声明齐全 → approval+attestation+发布 → provenance `operator_attested` → `require_builtin_voice` 通过 → `has_qualified_voice` 为真 |
| 反例 1（默认关闭） | 同文件的 `trust_declaration_missing` 用例：命名缺失声明，且零写入 |
| 反例 2（声明与事实不符） | 摘要 / 实例 / generation / fencing 四组参数化用例，各自命名失败项 |
| 反例 3（时效） | 窗口过期 / 超上限用例 |
| 反例 4（不能顶替 controller 语义） | 算力释放与 `execution_cleanup_pending` 的 domain 用例 + 运行时账本前后快照 |
| 反例 5（不能静默降级） | `require_native_ready` / probe 授权对自证实例仍拒绝；既有 controller 负例原样保持 |
| 反例 6（准入不吃无证据的行） | 伪造发布（含**只写资格行、不写 provenance 行**）→ `require_builtin_voice` 返回 `qualification_source_unverified`、Job 兜底不选中该行、公开 Job 答 422 并按状态点名修复动作；声明过期后同样拒绝且文案点名「声明已过期」 |
| 正例 2（controller 回归） | 同一文件用例：probe 支撑的发布只有带 provenance 才被接受；先断言无 provenance 时被拒，再写入探针授权与 provenance 后接受 |

## 9. 实现与验收状态

- 领域层：`speech/domain/operator_attestation.py`（声明/请求/窗口/跨事实校验、
  `build_operator_facts`）与 `speech/domain/builtin_voice_qualification.py` 的 facts 变体分派
  （探针变体逐字节不变）。
- 写入事务：`operator_attested_qualification.py`；CLI：`attest_builtin_voice.py`；
  读面：`qualification_provenance.py`；发布入口在 `builtin_voice_publication.py` 写 provenance。
- 迁移：`0055_operator_attestation`；授予：`builtin_voice_grants.py`。
- 准入判据：`speech/infrastructure/qualification_source_evidence.py`，由
  `require_builtin_voice`（同步 TTS 准入与启动门）与 `public_job_admission._custom_voice`
  共用，语义见 §3.6。
- 用例：`tests/unit/backend/test_operator_attested_qualification.py`（27 项）、
  `tests/integration/test_operator_attested_qualification_pg.py`（26 项，隔离 PostgreSQL）。
- 线上两跳端到端：`tests/integration/test_public_job_admission_http_pg.py`
  （真实 `create_app` + httpx：422 + 点名文案 + 删除 provenance 后同请求被接受）与
  `tests/integration/test_tts_factory_pg.py`（真实 `create_app_from_env` + httpx：404 +
  日志 `qualification_source_unverified`）。
- 部署装配：`sn_operator_attestation` 由
  [最小部署 §6](../operations/compose-minimal.md#外部托管引擎的资格声明角色-sn_operator_attestation)
  在新建与升级两条路径创建并按 `operator_attestation_writer_grants` 授权，两个路径的存在性由
  `tests/ci/test_reference_deploy_dsn_coverage.py` 的 `UPGRADE_PATH_ROLES` 与
  `ROLE_GRANT_BINDINGS` 钉住。口令由 §6 的十六键生成步骤写入 `deploy/compose.env`；连接地址
  是 host 侧导出（host 端口），`deploy/compose.yaml` 不转发它，`deploy/compose.env.example`
  只有口令占位。资格来源审计用另一个只读身份 `sn_qualification_reader`
  （`qualification_reader_grants`）执行，DSN 同样是 host 侧导出。未导出连接地址时命令在连接前退出并点名
  `SPEECHNEXUS_OPERATOR_ATTESTATION_WRITER_DATABASE_URL`（退出码 `2`），这是本路径唯一的
  「未启用」形态，不会降级为 controller 观测。

## 10. 未覆盖与风险

- 本设计不取证真实外部引擎的 `200`；GPU 通道另行取证。资格成立 ≠ 引擎可服务，
  gateway policy 只证明认证后读取的 live 配置。
- 运维自证把「引擎确实以声明的镜像与实例身份在服务」的信任交给了部署方；
  fact 记录只能证明声明自洽且与持久注册/release/gateway 事实一致，不能证明运行时字节。
- 外部引擎若不经 execution gateway 被调用，TTS 请求本身仍然失败；本路径不改变执行链。
- 撤销沿用既有 `speechnexus-revoke-builtin-voice`；撤销后新准入拒绝，已持有执行身份的重放语义不变。
