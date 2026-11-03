# Builtin Voice 资格边界（实施中）

当前只规划 `Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice` / `Vivian`。来源批准和执行资格是不同事实：来源批准固定来源修订、摘要、speaker 与期限；独立 qualification producer 核验实际原证据后记录完整 `BuiltinVoiceFacts`。预设要求 `builtin-customvoice-v1` / `1` 以及八项固定检查，生产者未取得完整证据时不得写入 approval。现有探针不自动等于此资格。

数据库 owner 负责部署角色，应用运行身份不是 owner。`qualification_writer_grants` 只供独立可信生产者；普通业务角色不得继承该角色，也不得获得审批或发布写权限。`qualification_reader_grants` 为准入提供只读权限；`builtin_publisher_grants` 需与现有 publication writer grants 组合，并且不得授予来源或资格审批写权限。不能把调用方 JSON、CLI 参数或 `passed` 自述当作来源批准。现阶段这些是权限声明，尚须部署管理员实际配置并核验角色继承、PUBLIC 权限与真实拒绝行为。

资格和来源撤销是追加事实，写入前必须在同一事务持有对应 voice 的 `speechnexus:catalog:<schema>:voice:<voice_id>` advisory lock，提交后新准入拒绝；已持有的原执行身份重放应先于资格检查。每个来源批准固定一个 voice_id，复合外键禁止其他 voice 关联；撤销者读取该不可变 voice_id 后持对应锁。该范围不提供跨声音的上游来源或密钥全局撤销。下述 producer 已有受控实现，真实运行资格仍须取证；本文件不授权人工 SQL 替代生产流程。

有权来源审批操作员先实际审查并决定来源是否可用，再以独立受限身份调用 `speechnexus-approve-builtin-source` 记录该决定；命令本身不制造来源批准，也不授予模型、探针或发布资格。通过仓库外 `SPEECHNEXUS_SOURCE_APPROVAL_WRITER_DATABASE_URL` 配置使用 `builtin_source_approval_writer_grants` 的身份，该身份不能是数据库或表 owner，也不能拥有资格审批或发布写权限。明确提供 `--database`、`--schema`、`--installation-id`、`--source-approval-id`、`--voice-id`、`--source-revision`、`--source-digest`、`--native-speaker-id Vivian` 和 `--expires-at`；时间必须带时区，最多微秒精度。来源修订是非敏感标识，不填 URL 或凭据。审批结果未知时，对同一目标重放原 ID 与全部原字段；已撤销或到期的原回执仍可恢复，但不会恢复资格。随后才运行受控 probe，取得执行资格后使用发布入口。

已获撤销授权的操作员使用 `speechnexus-revoke-builtin-voice`；连接通过仓库外的 `SPEECHNEXUS_QUALIFICATION_WRITER_DATABASE_URL` 配置为受限可信 writer。`--database`、`--schema`、`--installation-id` 必须是事先核对的目标，命令会在事务内复核。`--kind source` 指定来源批准 ID，`--kind qualification` 指定资格批准 ID；同时明确 `--approval-id` 和 `--reason`。回执未知时核对原目标并重放相同参数；同一批准的撤销不能用另一 reason 覆写。撤销不宣称现有引擎已停止，也不释放预算。

`speechnexus-publish-builtin-voice` 必须读取事先存在的独立批准；验证固定检查、来源记录、binding 公开身份与数据库证据，并对提供的原文件调用 release 验证器。binding release 验证仅证明制品内容一致，不证明物理执行或授予音色资格。发布同一事务记录公开文档、私有 speaker 映射、publication、head 和完整 intent；原身份重放先于当前资格判断。已有 generic voice publication 不能补写资格冒充本入口。

`tests/integration/test_builtin_voice_qualification.py` 在隔离 PostgreSQL 上使用受限发布角色、合成 TTS 证据和真实 release 验证器，覆盖发布与原意图重放、审批写权限拒绝、证据篡改回滚，以及独立连接观察到的撤销锁等待。`test_builtin_voice_revocation.py` 验证受限 writer 的两类撤销、真正提交后模拟丢回执和原身份恢复。此范围不证明真实模型资格、提交后的实际传输丢包、双库、物理多机或正式消费者装配；producer 的实现与验证范围见下文。

## 资格探针准入（开发中）

`speechnexus-run-builtin-qualification` 是独立资格操作员入口，使用 `SPEECHNEXUS_QUALIFICATION_ADMISSION_DATABASE_URL` 受限身份、已有私有 recipe 以及 owner-only 原意图文件。该端口核对预先独立批准的一次性 probe authorization、来源批准、binding publication/预备报告、目标 installation 与固定实例 generation，执行正常完整配方预算准入；普通业务角色不得获得该端口所需权限。固定探针文本、单槽、WAV/24 kHz 和十秒上限不能用作任意合成入口。新准入在预算和结算锁等待结束后、提交前再次检查来源与一次性授权的有效期；既有回执重放不受当前过期或撤销阻断。同键只按原意图恢复，未知结果不得创建新身份。

准入会核对当前 controller READY 观察中的镜像、模板及控制器制品摘要、native auth、实例 generation 和 fencing；缺失或过期即回滚。`--issue` 使用独立资格 writer 和可信操作员提供的模板、controller 清单、模型 pin 与 release 原文件，核对来源批准和实读模型字节后签发一次性授权。`--execute` 根据已保存的原身份通过网关提交或恢复，并读取版本固定的输出和停止证据；加 `--approval-id` 由程序核验并写入资格批准。原始输入或执行结果不完整时不得写批准；`qualification=pending` 不代表音色已获资格。结果未知时重放同一 approval、authorization、原意图、recipe 和四份原文件，系统先查询原回执，输入变化则冲突。真实模型/引擎运行还需单独批准。

当前受控入口要求使用已核验的源码 checkout：离线模型检查复用 `scripts/dev-env/verify-file-asr-artifacts.py`，本次验证文件 SHA-256 为 `44c92419824c1382fcf8847135c7f39ee18de8b2b9beeb7fd4d054b023185587`。脚本缺失时检查失败关闭；当前 wheel 不包含此脚本，不能作为已完成的 wheel 部署交付。隔离 PostgreSQL 上的新链测试使用合成模型文件、合成网关及终止证据，并运行真实 release 验证器；尚未实连网关或引擎，不能作为真实物理资格。

controller 制品摘要是受信不可变部署前提下的磁盘 Python 源或安装制品清单摘要，不证明已加载字节码、依赖或整个镜像。网关证据仅反映认证后读取时的 live policy 和映射，不代表 GPU 健康或声音资格。
