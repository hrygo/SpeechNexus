# Binding 停用 worker

worker 针对显式配置的一组 gateway/node 扫描持久停用记录，取得原始 gateway 提交身份，发起幂等 abort，并分别调用既有租约事务结算匹配的未释放租约。执行处于 unknown 或隔离状态时，须等待原事务取得所需证据；abort 请求本身不是停止证明。

在**同一** PostgreSQL 数据库和 schema 中准备两个不同的非 owner 登录角色。迁移 owner 使用 `speechnexus_backend.speech.infrastructure.binding_disable_targets_schema` 的 `disable_coordinator_grants(schema, scanner_role)` 和 `disable_settlement_grants(schema, settlement_role)` 生成授权 SQL，由操作员审阅并执行；worker 不会自行授权。结算角色仅需既有释放事务声明的权限，不得继承扫描角色；扫描角色也不得继承结算角色。启动时检查两者的权限，包括禁止的额外表或列权限，并比较数据库名、服务端地址和端口及登录/有效身份。这些字段相同不证明代理或复制环境只有一个权威数据库；部署仍须独立确认两条连接访问同一权威实例。

配置 `SPEECHNEXUS_PROCESS_ROLE=binding-disable-worker`、扫描角色的 `SPEECHNEXUS_DISABLE_DATABASE_URL`、结算角色的 `SPEECHNEXUS_DISABLE_SETTLEMENT_DATABASE_URL`、`SPEECHNEXUS_DATABASE_SCHEMA`、`SPEECHNEXUS_DISABLE_GATEWAY_ID`、`SPEECHNEXUS_DISABLE_NODE_ID`、`SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL` 和 `SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL`。还须显式配置有界的 `SPEECHNEXUS_DISABLE_GATEWAY_TIMEOUT_SECONDS`、`SPEECHNEXUS_DISABLE_STORAGE_TIMEOUT_SECONDS`、`SPEECHNEXUS_DISABLE_POLL_SECONDS`、`SPEECHNEXUS_DISABLE_BATCH_SIZE` 和 `SPEECHNEXUS_DISABLE_WAIT_TIMEOUT_MS`。凭据通过部署批准的秘密管理机制提供。启动命令为 `python -m speechnexus_backend.binding_disable_worker`。

每轮重新访问持久记录，并按原 lease ID 分页处理匹配 gateway/node 的租约。租约服务与 gateway 提交串行化：持有租约锁时没有提交记录才允许按未提交路径释放；已提交或隔离的执行需要该服务要求的证据。释放事务提交结果未知时按同一 lease ID 查询。多个 worker 可以重复访问同一身份，但结果由租约行锁和幂等释放决定。一轮成功或空页不证明所有引擎已停止或取消已完成。

如果 gateway 记录 abort 外发后、实际发送前崩溃，原结果未知，当前引擎适配器不能安全重放外部 abort。应保持执行隔离和容量占用，按[对账恢复](reconciliation-recovery.md)使用原身份和可信节点停止证据恢复。不能从 abort 回执推断已停止，也不能未经所需 owner 授权自动停止整个实例。
