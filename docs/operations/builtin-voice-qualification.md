# Builtin Voice 资格边界

当前受控资格范围是 `Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice` / `Vivian`；来源批准、资格探针和发布入口已有实现，实际可用性仍取决于部署配置和有效资格。来源批准和执行资格是不同事实：来源批准固定来源修订、摘要、speaker 与期限；独立 qualification producer 核验实际原证据后记录完整 `BuiltinVoiceFacts`。

资格有两个互相独立、机器可区分且持久化的来源：node-controller 观测的受控探针（`builtin-customvoice-v1` / `1`，八项固定检查，见下文「资格探针准入」）与外部托管引擎的 operator 声明（`operator-attested-customvoice-v1`，见「外部托管引擎的 operator 声明资格」）。两者写入同一个 approval 账本和同一份公开资格，但 facts 键集互斥、verifier 不同，发布时在 `speech_builtin_voice_qualification_provenance` 记录本次资格由哪个来源产出。生产者未取得所选来源要求的完整证据时不得写入 approval；普通探针通过不自动等于此资格。

公开 `backend_unavailable` 不携带内部拒绝原因。隔离验收须同时保全有界私有应用日志：HTTP 记录服务端生成的 execution UUID 和固定失败类别；普通准入记录原 operation、失败阶段及受控拒绝码，未知码仅记 `unknown_code`；异常存储记录异常类型和五位 SQLSTATE，不记录异常正文、SQL、参数、请求内容、客户端头或连接串。仅有 503 或准入记录为空不能证明 readiness 过期；须结合原持久有效期和具体失败证据定位。诊断不重发未知请求，也不改变停止与结算要求。

数据库 owner 负责部署角色，应用运行身份不是 owner。`qualification_writer_grants` 供独立可信资格生产者和获授权的撤销操作使用；它还包含来源批准插入以及来源、资格撤销权限，并非仅能写资格批准。普通业务角色不得继承该角色，也不得获得审批或发布写权限。`qualification_reader_grants` 为准入提供只读权限；`builtin_publisher_grants` 需与现有 publication writer grants 组合，并且不得授予来源或资格审批写权限。不能把调用方 JSON、CLI 参数或 `passed` 自述当作来源批准。现阶段这些是权限声明，尚须部署管理员实际配置并核验角色继承、PUBLIC 权限与真实拒绝行为。

资格和来源撤销是追加事实，写入前必须在同一事务持有对应 voice 的 `speechnexus:catalog:<schema>:voice:<voice_id>` advisory lock，提交后新准入拒绝；已持有的原执行身份重放应先于资格检查。每个来源批准固定一个 voice_id，复合外键禁止其他 voice 关联；撤销者读取该不可变 voice_id 后持对应锁。该范围不提供跨声音的上游来源或密钥全局撤销。下述 producer 已有受控实现，固定配置的真实运行证据见文末；其他部署与模型组合仍须分别取证。本文件不授权人工 SQL 替代生产流程。

有权来源审批操作员先实际审查并决定来源是否可用，再以独立受限身份调用 `speechnexus-approve-builtin-source` 记录该决定；命令本身不制造来源批准，也不授予模型、探针或发布资格。通过仓库外 `SPEECHNEXUS_SOURCE_APPROVAL_WRITER_DATABASE_URL` 配置使用 `builtin_source_approval_writer_grants` 的身份，该身份不能是数据库或表 owner，也不能拥有资格审批或发布写权限。明确提供 `--database`、`--schema`、`--installation-id`、`--source-approval-id`、`--voice-id`、`--source-revision`、`--source-digest`、`--native-speaker-id Vivian` 和 `--expires-at`；时间必须带时区，最多微秒精度。来源修订是非敏感标识，不填 URL 或凭据。审批结果未知时，对同一目标重放原 ID 与全部原字段；已撤销或到期的原回执仍可恢复，但不会恢复资格。随后才运行受控 probe，取得执行资格后使用发布入口。

已获撤销授权的操作员使用 `speechnexus-revoke-builtin-voice`；连接通过仓库外的 `SPEECHNEXUS_QUALIFICATION_WRITER_DATABASE_URL` 配置为受限可信 writer。`--database`、`--schema`、`--installation-id` 必须是事先核对的目标，命令会在事务内复核。`--kind source` 指定来源批准 ID，`--kind qualification` 指定资格批准 ID；同时明确 `--approval-id` 和 `--reason`。回执未知时核对原目标并重放相同参数；同一批准的撤销不能用另一 reason 覆写。撤销不宣称现有引擎已停止，也不释放预算。

`speechnexus-publish-builtin-voice` 必须读取事先存在的独立批准；验证固定检查、来源记录、binding 公开身份与数据库证据，并对提供的原文件调用 release 验证器。binding release 验证仅证明制品内容一致，不证明物理执行或授予音色资格。发布同一事务记录公开文档、私有 speaker 映射、publication、head 和完整 intent；原身份重放先于当前资格判断。已有 generic voice publication 不能补写资格冒充本入口。

来源批准、撤销和发布入口对已分类的业务拒绝返回退出码 `4`；存储不可用或结果未知的已分类分支返回 `5`，须核对原目标并按原身份及全部原输入恢复。参数或配置错误通常返回 `2`。这些退出码不覆盖所有未捕获异常；特别是命令未明确报告成功时，不能仅凭非零退出认定事务没有提交，也不能换身份重试。

`tests/integration/test_builtin_voice_qualification.py` 在隔离 PostgreSQL 上使用受限发布角色、合成 TTS 证据和真实 release 验证器，覆盖发布与原意图重放、审批写权限拒绝、证据篡改回滚，以及独立连接观察到的撤销锁等待。`test_builtin_voice_revocation.py` 验证受限 writer 的两类撤销、真正提交后模拟丢回执和原身份恢复。此范围不证明真实模型资格、提交后的实际传输丢包、双库、物理多机或正式消费者装配；producer 的实现与验证范围见下文。

## 资格探针准入

`speechnexus-run-builtin-qualification` 是独立资格操作员入口，使用 `SPEECHNEXUS_QUALIFICATION_ADMISSION_DATABASE_URL` 受限身份、已有私有 recipe 以及 owner-only 原意图文件。该端口核对预先独立批准的一次性 probe authorization、来源批准、binding publication/预备报告、目标 installation 与固定实例 generation，执行正常完整配方预算准入；普通业务角色不得获得该端口所需权限。固定探针文本、单槽、WAV/24 kHz 和十秒上限不能用作任意合成入口。新准入在预算和结算锁等待结束后、提交前再次检查来源与一次性授权的有效期；既有回执重放不受当前过期或撤销阻断。同键只按原意图恢复，未知结果不得创建新身份。

准入会核对当前 controller READY 观察中的镜像、模板及控制器制品摘要、native auth、实例 generation 和 fencing；缺失或过期即回滚。`--issue` 使用独立资格 writer 和可信操作员提供的授权文件、模板、controller 清单、模型 pin、模型目录与 release 原文件，核对来源批准和实读模型字节后签发一次性授权。`--execute` 根据已保存的原身份通过网关提交或恢复，并读取版本固定的输出和停止证据；加 `--approval-id` 由程序核验并写入资格批准。原始输入或执行结果不完整时不得写批准；`qualification=pending` 不代表音色已获资格。结果未知时保留并重放原 approval、authorization、意图和 recipe；批准恢复还需原 pin、manifest、report 和 scenario 文件，重新执行 `--issue` 时还须保留原授权文件、模板、controller 清单和模型目录。系统先查询适用的原回执，输入变化则冲突。真实模型/引擎运行还需单独批准。

不加 `--execute` 时，命令仍可执行准入并返回 `qualification=pending`，不会执行探针或写资格批准；`--approval-id` 必须配合 `--execute`。已分类的非 unknown 准入结果返回退出码 `4`，unknown 或执行阶段异常返回 `5`。输入处理阶段的部分异常返回 `2`，但 `--issue` 可能已在此前提交授权，因此任何未明确完成的运行都须核对原授权和准入身份，再按原输入恢复，不能仅凭退出码认定没有持久化。

执行证据检查要求原提交已成功，持久身份与授权一致，并读取匹配的 `engine_terminal_response`：执行停止和资源释放均为真。版本固定的 WAV 必须是完整、未压缩的 24 kHz 单声道 PCM16，帧数与持久生成采样数一致且不超过冻结上限；文件字节数也受原计划上限约束。这些检查针对该次执行，不证明整实例停止或租约已结算。

当前受控入口要求使用已核验的源码 checkout：离线模型检查复用 `scripts/dev-env/verify-file-asr-artifacts.py`，当次验证文件 SHA-256 为 `44c92419824c1382fcf8847135c7f39ee18de8b2b9beeb7fd4d054b023185587`，使用时须核对实际版本。脚本缺失时检查失败关闭；当前 wheel 不包含此脚本，不能作为已完成的 wheel 部署交付。隔离 PostgreSQL 的组件测试使用合成模型文件、合成网关及终止证据，不证明真实物理资格；后续真实引擎的受控探测及范围见[2026-11-03 窗口记录](../archive/audits/2026-11-03-controlled-tts-prepare.md)。

controller 制品摘要是受信不可变部署前提下的磁盘 Python 源或安装制品清单摘要，不证明已加载字节码、依赖或整个镜像。网关证据仅反映认证后读取时的 live policy 和映射，不代表 GPU 健康或声音资格。

## 外部托管引擎的 operator 声明资格

`deploy/compose.yaml` 的 bootstrap 明确「engine lifecycle is operator/external」，即没有 node-controller 参与该实例生命周期。这条路径为这类部署提供受控资格，且不改变 controller 观测路径的任何行为。

**信任默认关闭。** 只有部署方以独立私有文件显式声明信任后，才可能写入声明资格：声明缺失即 `error: trust_declaration_missing`（退出码 `4`），不写任何行。代码不会因为「当前没有 node-controller」而自动降级为声明，也不会把探针失败转成声明成功。声明文件为 owner-only、位于仓库外，格式 `speechnexus-operator-attested-engine-v1`，至少固定：目标 `installation`（database/schema/installation_id）、`scope.lifecycle_controller='operator_external'`、`trusted_instances`（逐实例白名单）、`acknowledged_gaps`（必须完整覆盖本路径不提供的 controller 能力）、`max_attestation_validity_seconds`（上限 30 天）与 `declared_by`。

**声明请求固定可核验事实。** 请求文件格式 `speechnexus-operator-attested-qualification-v1`，声明 voice/revision、binding 与 binding revision、instance 与 generation、fencing token、模板身份、镜像摘要、模型 id/revision/制品摘要、能力声明与有效期窗口，并内嵌模板原件（`speechnexus-template-v1`）与外部生命周期原件（`speechnexus-external-lifecycle-v1`）；两个原件的规范摘要（`sort_keys` 紧凑 JSON 的 `sha256:`）记录在案，任何第三方可用同一文件重算核对。

`speechnexus-attest-builtin-voice` 在单个事务内依次核对：installation 目标与声明一致；来源批准存在、未撤销且未过期；binding revision 已发布、未停用，release 原文件通过正式 release 验证器（`verify_binding_release_evidence`）；注册账本中的 instance/generation/state/host/gateway/pool/binding_revision/template/config digest/fencing/owner 期限/policy revision 与声明和配方一致；模板与生命周期原件的身份、模型 pin 与 stage pin 一致；声明能力是 binding 参数域的子集；认证网关的 live policy 映射（含 `gateway_policy_digest`）与持久事实一致且观测时间在 60 秒内；有效期窗口不超过声明上限、来源有效期与 release 有效期。每一项不满足都以具名错误码退出（`trust_declaration_target_mismatch`、`trust_declaration_instance_not_trusted`、`image_digest_mismatch`、`template_identity_mismatch`、`instance_generation_mismatch`、`stale_fencing_token`、`attestation_window_expired`、`attestation_window_too_long`、`gateway_policy_evidence_unavailable`、`model_artifact_digest_mismatch`、`capability_outside_binding_domains`、`source_approval_unavailable`、`binding_release_evidence_mismatch`、`writer_identity_forbidden`、`attestation_conflict`），并回滚到零写入。

**写入身份最小化。** 命令使用仓库外 `SPEECHNEXUS_OPERATOR_ATTESTATION_WRITER_DATABASE_URL` 配置的独立非 owner 身份，权限由 `operator_attestation_writer_grants` 声明：只读核对所需的注册与发布事实，追加一条 approval（探针列为 `NULL`）和一条声明记录，不持有任何表的 `UPDATE`/`DELETE`/`TRUNCATE`，也没有资格、发布或 publication head 写权限。持有发布 head 权限的身份会被 `writer_identity_forbidden` 拒绝，因为重指发布 head 可以撤回公开资格。

**持久与读面。** 迁移 `0055_operator_attestation` 新增 `speech_builtin_voice_operator_attestations`（按 approval 唯一，含 instance/generation/fencing/策略摘要/四个制品摘要/窗口/声明原件与其摘要/声明人）与 `speech_builtin_voice_qualification_provenance`（每个已发布资格一行，`source_kind` 取 `controller_observed` 或 `operator_attested`，后者必须带 attestation 外键）。发布入口对两个来源都写 provenance，并在写入前复核算出声明的 approval 与声明记录逐项一致，不一致即 `attestation_mismatch` 回滚。`speechnexus-qualification-provenance` 只读打印每个语音的 `source_kind`、`provenance_recorded` 与声明细节；迁移 `0055` 之前已发布的资格没有 provenance 行，报告为 `evidence=legacy_pre_0055` 且 `source_kind=controller_observed`（当时不存在声明路径），不推断、不回填、不改写既有记录。`--fail-on-expired-attestation` 在声明窗口已过时以退出码 `3` 失败。

**不开放的能力。** 声明不是 controller 观测，因此不放行：`require_native_ready` 与探针签发（仍要求 node-controller 的 native runtime 证据，缺失即 `native_runtime_evidence_unavailable`）；算力预算归还与租约结算（`release()` 仍要求授权 stop kind，缺少实测停止即 `execution_cleanup_pending`）；`execution_cleanup_pending` 的清偿；实例生命周期收敛与新 generation（仍要求上一代 `stopped` 且预算 `released`）；`execution_stop_evidence`、`runtime_intent_deliveries` 与 `admission_leases` 的任何写入。声明事务的写目标是且只是 approval 与声明记录两行；`tests/integration/test_operator_attested_qualification_pg.py` 对运行态账本做前后快照以固定这一点。

撤销沿用既有入口：`speechnexus-revoke-builtin-voice --kind qualification --approval-id <声明 approval>` 追加撤销事实，此后新准入拒绝；声明记录本身保留为已发生事实，不被改写。迁移的 `downgrade` 在存在声明历史时拒绝执行（`Operator attestation history requires preservation`），不存在时先删 provenance 再删声明表。

## 正式公开 SDK 验收

资格 probe、公开 SDK 和最终停止结算分别取证。真实引擎仍需单独运行授权；下面的
客户端既不启动引擎，也不产生来源、资格、发布或预算事实。

运行前使用锁定依赖，完成来源决定、受限角色、原文件/摘要、安装身份、配方及控制器
退出路径的检查。公开请求的 `model` 使用已发布 **binding_id**，不能使用其
`logical_model` 展示名；私有 TTS recipe 的 `model_id` 必须与公开请求一致，并按
正式 loader 的算法计算 `recipe_digest`。资格 probe 的原 recipe、原身份和字节保留，
不因客户端配置修正而覆盖。

首次 prepare 前，还须核对本轮私有 gateway context、gateway recovery、API context 已生成，
其中 installation、binding revision、generation、独立角色和网关认证均与本轮记录一致。
仅脚本导入或控制器健康检查通过，不能证明这些上下文齐备。固定 SDK 文本目前为
27 字符，必须同时处于已批准及公开声音能力的输入上限内；不为验收扩大声音资格。

API 使用正式组合根；通过已有的仓库外配置注入 speech recipe、独立 settlement DSN、
对象存储和网关配置，不能用基础 `app.create_app` 创建一个未装 speech 的临时服务：

```bash
uv run uvicorn speechnexus_backend.transcription_runtime:create_app_from_env --factory --host 127.0.0.1 --port 18430
```

`/healthz` 和 `/readyz` 不证明 speech 已装配。启动后先核对资格 probe 的原 lease 已经
由正式 settlement worker 释放，不能通过修改计数腾槽。核对当前持久 readiness 期限；
准入到期会拒绝，即使实例仍是 `ready`。将客户端截止设为 readiness、recipe、来源/资格
及本轮运行窗口最早的截止，预留完成请求和可信停止/结算的时间；不足则不发新请求。
不能延长旧证据 TTL 或重写 READY 时间来通过验收。

资格 probe 已结算且其余准入写入静止后，保存标准 SDK 前的具名执行快照。以下关联工具
只读正式数据库、网关回执和版本固定对象，不会提交合成或产生停止/释放事实；
`TTS_PRIVATE_TASK_ROOT` 是本轮已核对的仓库外私有根目录，包含私有 `private/` 上下文和
`sdk-evidence/` 输出目录，不是新授权。工具先校验数据库、schema、installation 与只读事务。
可信读取流程须先生成 `private/public-phase-readiness.json`，包含本轮 instance、generation 和
原 verified/expiry 时间；关联工具在 before 固化该原文件摘要，after 精确核对同一记录，
不刷新 READY，也不因只读补验发生于到期后而新建执行。SDK 原请求摘要及有效命令同时核对。

```bash
uv run python scripts/dev-env/verify-file-tts-association.py \
  --task-root "$TTS_PRIVATE_TASK_ROOT" --phase before
```

```bash
uv run python scripts/dev-env/verify-file-tts-sdk.py \
  --base-url http://127.0.0.1:18430/v1 \
  --binding-id "$TTS_BINDING_ID" \
  --workspace-id "$TTS_WORKSPACE_ID" \
  --voice-id "$TTS_VOICE_ID" \
  --voice-revision "$TTS_VOICE_REVISION" \
  --deadline-at "$TTS_ACCEPTANCE_DEADLINE" \
  --timeout-seconds 15 \
  --output-dir "$TTS_PRIVATE_EVIDENCE_DIRECTORY"
```

这些变量是已核对的本轮输入，不是客户端授予的权限或资格。输出目录必须是新目录，
且位于仓库外。客户端使用固定合成英文、标准 OpenAI SDK、单次请求和有界 WAV 读取，
保留开始标记、成功音频/摘要或脱敏失败状态；退出非零不是已知未提交的证明。遇到超时、
断线或缺失回执，先由操作员核对正式数据库中唯一的原 operation / attempt / submit，
保全其完整原命令。客户端关联 ID、X-Request-ID 和 client_reference 均不等于内部执行
身份，当前 SpeechPlan 也不持久化 client_reference，不能据此直接恢复。无法唯一关联时
保持 unknown，不新建目录、换 client_reference 或原样盲重发。

上述 SDK 的 output-dir 须对应本轮 `TTS_PRIVATE_TASK_ROOT/sdk-evidence`。SDK 成功且原租约
已结算后，在网关查询与版本固定对象仍可读取时执行关联检查；身份比较使用显式列名，
不依赖 JSON 对象键序。不能凭 HTTP 200 或 operation 数量代替该检查。

```bash
uv run python scripts/dev-env/verify-file-tts-association.py \
  --task-root "$TTS_PRIVATE_TASK_ROOT" --phase after
```

公开成功后仍须检查 operation / submit / lease 与 voice/binding revision、generation、
结果摘要和版本化对象的关联，检查上限、未发布/退役、失败及未知恢复语义。客户端
不验证这些数据库和引擎事实，其一次 200 不结算整个 Goal。

完成业务请求后先等待原 lease 已释放、submit 已终态，再在允许停止的状态下，经正式
事务登记原 stop intent/outbox，然后调用 controller；创建 stop 会进入 draining，不能
在资格 probe / SDK 前提前登记。停止先完成进程退出和资源释放双证据，再正式结算
实例基础预算，并核对所有 held/quarantined 维度、slots 和 quota。独立安全守护仍须
预先准备；若守护先物理停止，如实标注事后协调，不能补记成正常先持久化再外发。

收尾分开判断“可信停止/预算释放”和“验收证据齐全”。关联检查异常时仍按原身份停止、
结算，但先保留专用数据库与对象，在剩余窗口内只读核实；修正只读工具不允许重发 SDK。
删除数据库、对象存储和临时目录前，交付负责人须核验关联结果及原停止、租约重放和
全零预算证据已经可靠保全。接收未完成不能阻止已授权窗口的硬截止：若截止前仍缺证据，
导出可安全保留的事实与缺口后按期清理，如实保持 Goal 未完成，不补签丢失事实。

2026-11-03 六次窗口及各自证据见 [受控验收记录](../archive/audits/2026-11-03-controlled-tts-prepare.md)。
第四窗口确认固定文本超出资格；第五窗口取得真实标准 SDK 200/WAV，但关联脚本的 JSON
键序缺陷及临时账本清理导致该窗口的有效命令与结果完整关联缺证。第六窗口使用新的受控
证据验证了原请求、执行身份、网关回执、固定对象与结算关联；其范围仍限固定声音、单节点、
单实例及短英文同步链。第三窗口在有效 readiness 内的 503 保留历史事实，不能将 readiness
过期作为全部现场失败的解释。
首次原生资格、正式资格 probe 或合成 transport 测试都不能替代标准 SDK 的真实链路验收。
