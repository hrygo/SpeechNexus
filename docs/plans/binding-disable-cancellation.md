# Binding disable：取消协调接续方案

目标是阻止目标 revision 的新使用，协调已准入的原执行停止；无法确认清理时保留隔离占用。当前需求及验收见 [X-BINDING-STOP](../prd.md#x-binding-stop) 和 [M-DISABLE](../prd.md#m-disable)。原 [#273](https://github.com/hrygo/SpeechNexus/issues/273) 已取消排期，其 AC3 仅供追溯，关闭不表示功能完成。公开回执报告清理待确认，下面的合成引擎证据不能证明真实引擎已经停止。

## 已实现的前置和运行条件

- disable 的生命周期事件、退役事实和原取消意图在同一 PostgreSQL 事务写入；Job 创建与 binding 选择变更使用同一锁，并在锁内重新验证所选 revision。原幂等键重放先查询已有 Job。同步转写启用 binding 禁用屏障：先查询原准入，再在 binding 锁内复查原身份和禁用状态；已有原身份重放不等同于新的准入。
- 0025 增加按原 lifecycle event 和 submit 身份保存的逐目标账本及 backend abort 意图。独立 `binding_disable_worker` 限定自身 gateway/node，扫描原身份并使用单独的受限角色调用既有租约释放事务；对已记录的 abort 目标先查询，再以同一原意图调用幂等入口。Compose 的 `binding-disable` profile 需要运维显式启用和配置，默认部署不会启动该 worker。
- 0027 增加 Gateway abort 原请求及 dispatch 标记；`/internal/v1/executions/{submit_intent_id}/abort-intents` 只对完全相同的原请求提供重放。同键异意图冲突；dispatch 标记和 unknown quarantine 同事务提交，未取得可信停止证据不能释放资源。`/abort` 与 `/abort-intents` 共用持久取消接收及原意图冲突规则；新 worker 使用后者，不以旧端点作为回退。Gateway 启动时检查持久取消所需权限，不满足时两个取消入口均返回不可用。
- 隔离 PostgreSQL 集成测试设计覆盖受限角色、Job 与同步 ASR 的准入屏障、Gateway 提交屏障、原键重放、目标恢复和 dispatch 标记后的故障窗口。`test_disable_composition_root_pg.py` 使用 worker 和 Gateway 组合根及 loopback HTTP 测试取消和重启重放；引擎是合成适配器，不能证明真实引擎停止或多机行为。实际通过与否须核对指定候选和环境的测试结果。

## 固定事实和边界

- 原 disable 命令及其幂等键摘要、完整参数、所选 `binding_id`、`revision`、`revision_id` 和 lifecycle event 身份必须持久化；摘要不能代替命令正文。不要把客户端凭据或内容写入任务或日志。
- 对已创建 submit 的目标，账本固定 `submit_intent_id`、operation、attempt、lease、stage、gateway、node、instance/generation、binding revision、fencing token、abort 等待上限；这些记录和请求回执都不是 stop 证据。无 submit 的 lease 需走未外发证据路径，不能伪造 submit 身份。
- disable 的关闭选择、生命周期事件及取消意图应由同一数据库事务落下；事务外发 abort。事务提交结果未知后必须以原身份查询，不能换键重建任务。
- 调用方可看到的状态必须区分受理、清理未确认及完成；在契约、持久任务、结果查询和消费者接线之前，不应声称取消或释放完成。现行 503 `binding_result_unknown` 不能冒充确定未执行的拒绝。

## 实施与验收检查项

以下列出整条链路的检查项，已实现的部分及其证据范围见上文；不能仅凭清单判断完成状态。

1. 冻结兼容策略与状态机：选择异步受理/查询的公开结果、错误和幂等重放，更新 canonical、生成消费者及文档。已有同键冲突、历史事件、旧客户端和回退路径必须单独定义。没有一致可观察状态前，现有 `disabled` 回执不能表示 AC3 完成。
2. 定义按事件固定的 durable intent 和逐目标账本，以及最小写者/读取者权限、不可覆盖的原始参数、索引、版本/claim 和重启扫描。任务与事件在 disable 事务内原子写入；可恢复处理依赖原身份和持久状态，不依赖进程内通知。追加迁移及降级保护必须保留未处理任务和历史证据。
3. 核验同一数据库上的选择、准入与 gateway submit 屏障及锁序。public Job 已在写入前取得 binding 锁并重新校验；一次锁外事前读取不能代替该原子资格检查。扫描仍须处理屏障前在途及竞争中的创建和提交，确定无遗漏后才可结束枚举。持久账本的索引和逐批处理不得把长扫描或网络请求放进长事务。
4. 在受信调度器中从权威 `admission_attempts`、lease 和 submit 关联读取并核实完整原身份，将目标及固定 abort 命令持久化后，事务外调用正确 gateway 的 `abortExecution`。未发出、已发出、结果未知与终态分别处理；重放只查询/重试原意图，不能启动新的执行。`resource_force_disabled` 是已有内部 abort 原因，不是停止证明。
5. gateway 确认停止后仍须按租约事务验证匹配的停止和资源释放事实；未知或不可证实时保持/转入 quarantine 且不返还预算。`quarantine_lease` 需要 gateway 已记录的 unknown/quarantined 状态，不能以 HTTP 超时伪造该前提。取消与完成竞争时保留权威事实与终态，不改写成功或丢弃资源账目。

## 验收及依赖

- 在隔离 PostgreSQL 用独立连接验证 disable 与 Job 创建、准入、提交的关键交错、锁等待、回滚、同键重放、提交后丢确认及重启；检查目标 revision 精确覆盖且其他 binding 不受影响。
- 对 gateway 已确认停止、拒绝、丢回复、未知、错误 attempt/generation/token、迟到完成、未发出执行分别测试原身份、持久状态、停止证据、隔离与预算。使用真实 PG/InnoDB 协调路径验证证据到达前后账目；单次 fake 返回不构成停止证明。
- 接入实际消费者与已批准隔离服务后验证端到端；当前 file worker abort 只取消本地任务并返回无已验证 stop 的结果，不能用于证明真实引擎停止。真实设备、跨节点和故障接管另行验证。

本方案仍需完整的权威参数和配方 resolver、可查询的公开清理完成状态，以及真实引擎和多机证据。持久接收后若尚未取得 dispatch 标记，原意图重放仍可尝试取得唯一外发资格；若已提交 dispatch 标记却在外发前崩溃，当前只能保持 unknown 并查询原身份，不能保证自动发出取消。现有公开 `disabled` 回执只证明选择关闭，不证明停止或资源释放。受信节点停止证据及恢复流程见[协调恢复手册](../operations/reconciliation-recovery.md)。
