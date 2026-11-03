# Builtin Voice 资格边界（实施中）

当前只规划 `Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice` / `Vivian`。来源批准和执行资格是不同事实：来源批准固定来源修订、摘要、speaker 与期限；独立 qualification producer 核验实际原证据后记录完整 `BuiltinVoiceFacts`。预设要求 `builtin-customvoice-v1` / `1` 以及八项固定检查，生产者未取得完整证据时不得写入 approval。现有探针不自动等于此资格。

数据库 owner 负责部署角色，应用运行身份不是 owner。`qualification_writer_grants` 只供独立可信生产者；普通业务角色不得继承该角色，也不得获得审批或发布写权限。`qualification_reader_grants` 为准入提供只读权限；`builtin_publisher_grants` 需与现有 publication writer grants 组合，并且不得授予来源或资格审批写权限。不能把调用方 JSON、CLI 参数或 `passed` 自述当作来源批准。现阶段这些是权限声明，尚须部署管理员实际配置并核验角色继承、PUBLIC 权限与真实拒绝行为。

资格和来源撤销是追加事实，写入前必须在同一事务持有对应 voice 的 `speechnexus:catalog:<schema>:voice:<voice_id>` advisory lock，提交后新准入拒绝；已持有的原执行身份重放应先于资格检查。每个来源批准固定一个 voice_id，复合外键禁止其他 voice 关联；撤销者读取该不可变 voice_id 后持对应锁。该范围不提供跨声音的上游来源或密钥全局撤销。可信批准 producer 尚未实现，本文件不授权人工 SQL 替代生产流程。

已获撤销授权的操作员使用 `speechnexus-revoke-builtin-voice`；连接通过仓库外的 `SPEECHNEXUS_QUALIFICATION_WRITER_DATABASE_URL` 配置为受限可信 writer。`--database`、`--schema`、`--installation-id` 必须是事先核对的目标，命令会在事务内复核。`--kind source` 指定来源批准 ID，`--kind qualification` 指定资格批准 ID；同时明确 `--approval-id` 和 `--reason`。回执未知时核对原目标并重放相同参数；同一批准的撤销不能用另一 reason 覆写。撤销不宣称现有引擎已停止，也不释放预算。

`speechnexus-publish-builtin-voice` 必须读取事先存在的独立批准；验证固定检查、来源记录、binding 公开身份与数据库证据，并对提供的原文件调用 release 验证器。binding release 验证仅证明制品内容一致，不证明物理执行或授予音色资格。发布同一事务记录公开文档、私有 speaker 映射、publication、head 和完整 intent；原身份重放先于当前资格判断。已有 generic voice publication 不能补写资格冒充本入口。

`tests/integration/test_builtin_voice_qualification.py` 在隔离 PostgreSQL 上使用受限发布角色、合成 TTS 证据和真实 release 验证器，覆盖发布与原意图重放、审批写权限拒绝、证据篡改回滚，以及独立连接观察到的撤销锁等待。`test_builtin_voice_revocation.py` 验证受限 writer 的两类撤销、真正提交后模拟丢回执和原身份恢复。此范围不证明真实模型资格、提交后的实际传输丢包、双库、物理多机或正式消费者装配；可信 producer 仍待实现。
