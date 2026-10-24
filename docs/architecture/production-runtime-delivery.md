# 原 runtime 意图持久投递

本单元消费 [runtime create 事务](production-runtime-intents.md)已提交的完整原意图，
属于 A01 #20 / A03 #22；不新增公开 API 或另一套 wire DTO。PostgreSQL 是唯一产品数据库，
InnoDB 仅验证相同协调 SQL、约束和最小权限。固定 HTTPS 规则见[客户端](runtime-controller-client.md)。

## 持久状态与恢复

`0008_runtime_delivery` 在冻结的0001–0007迁移后新增 A 私有表
`runtime_intent_deliveries`，与 outbox 事件一一对应。新的 create 在同一事务写入完整请求、
原摘要、outbox 和 unsent 账本；任何一步失败全部回滚。升级只把旧的未确认 runtime 事件
收纳为 unknown，不改写旧事件/Job/原请求；没有账本不能证明从未发送。迁移不访问控制器，
重复升级为 no-op，破坏性 downgrade 拒绝执行。

| 阶段 | worker 行为 | 能证明的事实 |
| --- | --- | --- |
| unsent | 认领后重新检查当前 head/generation、原 deadline、意图状态和预算形态 | 只有新建同事务记录证明尚未开始投递 |
| unknown | 只 GET 原 node/intent；首次允许 POST 前必须先提交该标记 | 可能发送，不能推断物理占用或不存在 |
| acknowledged | 原响应身份/绑定/内存等经既有 codec 验证后，同事务写账本与 outbox ACK | 控制器识别原身份；不证明 ready、资格、停止或释放 |
| blocked | 原记录损坏或身份/摘要不符，保留原数据供受控诊断 | 不外发、不重建默认值、不归还预算 |

fresh 请求的 before-wire 标记必须提交成功才获得一次 POST 许可；失去提交回执就没有许可。
任何已开始请求，即使本次调用报告尚未发送，也不能自动变回 unsent。unknown 可在原 deadline
之后查询；404、认证失败、HTTP取消、超时和 worker 退出都不是未外发或物理释放证明。
字节上限内仍可能有过深嵌套的损坏原记录；结构解码的 `RecursionError` 归一为
既有 invalid_record/blocked，保留原命令与预算，使后续健康事件可继续处理。
首次请求已过期或被当前 head 替代时也只查询，不再次启动旧实例。B 的持久 fence/generation
与 STOP tombstone 防止 stop 后迟到的 unseen prepare 复活；A 不修改 B 的实现。

认领只锁私有账本，按配置的目标节点过滤，使用 bounded lease、holder/version CAS 与
`SKIP LOCKED`。HTTP 完全在数据库事务之外。ACK 取得账本和 outbox 必要锁后再次核对
数据库时钟/租约，旧 holder 不能在实际锁等待后收尾；两份 ACK 同事务。存储只有数据库
保证已回滚的 deadlock/serialization 错误可有界重试，结果未知的连接/提交错误不重执行。
延迟查询保留 unknown 并设置有界 backoff；崩溃后租约到期允许其他 worker 查原身份。

发送前标记已经提交、但进程未真正发送的窗口会保守停在 unknown。GET 404 不能自动解除该
状态；需要后续可信节点/协调证据，不能为获得进展而补发新身份或释放预算。

升级编排须先停止旧版本 runtime create 写入，确认所有旧创建事务结束，再由独立迁移身份升级、
由部署身份核对并补齐 core create 与专用 delivery 角色授权，然后启用同版 writer/worker。
不能让不写新账本的旧 writer 在 adoption 后继续创建事件；本单元没有声明跨版本滚动写兼容。
若发现无账本的未确认事件，暂停相关写入并由 owner 核对原身份后保守补记 unknown，不能
在 worker 中自动补成 unsent。上述是发布顺序约束，本轮未执行部署或修改实际部署权限。

## 独立 worker 与显式配置

安装 backend wheel 后运行 `speechnexus-runtime-delivery-worker`；`--once` 只执行一个有界
批次。一个进程绑定一个控制器节点，可按节点部署多个副本；并发权威在共享 PG 租约，
不依赖进程内全局锁。该入口不随 API 隐式启动，不需要 S3，不执行 migration/grant。

以下变量必须显式设置，凭据由部署的秘密注入渠道提供，不写入仓库或命令日志：

| 环境变量 | 含义 |
| --- | --- |
| `SPEECHNEXUS_PROCESS_ROLE` | `job-worker` |
| `SPEECHNEXUS_NODE_ID` / `SPEECHNEXUS_INSTANCE_ID` | 本进程的节点与实例身份，组合为认领 holder |
| `SPEECHNEXUS_RUNTIME_DELIVERY_DATABASE_URL` | 显式 host/database/user 的专用非 owner PG 连接 |
| `SPEECHNEXUS_RUNTIME_DELIVERY_DATABASE_SCHEMA` | 已由迁移身份升级的明确 schema |
| `SPEECHNEXUS_RUNTIME_DELIVERY_TARGET_NODE_ID` | 原请求所属控制器节点 |
| `SPEECHNEXUS_RUNTIME_DELIVERY_CONTROLLER_ADDRESS` | 固定私有/loopback IP 字面量 |
| `SPEECHNEXUS_RUNTIME_DELIVERY_CONTROLLER_PORT` | HTTPS 端口，无隐式默认 |
| `SPEECHNEXUS_RUNTIME_DELIVERY_CONTROLLER_TOKEN` | 该节点组件 bearer token |
| `SPEECHNEXUS_RUNTIME_DELIVERY_CA_FILE` | 可选 CA 文件；省略时用系统信任，仍验证证书链及 IP SAN |

其余同前缀参数：`LEASE_SECONDS=30`、`CALL_TIMEOUT_SECONDS=3`、
`STORAGE_TIMEOUT_SECONDS=3`、`INTERVAL_SECONDS=1`、`RETRY_SECONDS=1`、`BATCH_SIZE=8`。
lease 必须大于 call + 3×storage + 1秒；单批1–32，所有时间有限并校验范围。
进程不读 dotenv，不从 queued command 选择端点，不使用代理、不跟随重定向或自动重试 POST。
SIGINT/SIGTERM 在原请求之间停止，当前有界调用完成后关闭自己的连接；不向控制器发 STOP。
输出只有批次计数和固定错误分类，不输出原正文、响应、内部地址或凭据。

`runtime_delivery_grants(schema, role)` 为独立部署/迁移身份生成最低授权：读取必要原意图、
head/实例列，更新私有投递状态和 outbox 的 delivered 时间；无 INSERT/DDL/DELETE、
原命令/摘要更新、预算/配额/拓扑修改权。core A create 角色仅增加新账本 INSERT，B 无账本
读取或锁定权。实际权限须用非 owner 连接验证；启动检查拒绝 owner/schema CREATE，
不宣称审计了所有继承和数据库全局权限。部署仍需独立核对授权。

## 验证与剩余依赖

共享 PG/InnoDB 测试覆盖新建原子性、并发认领、接管/旧票据拒绝、实际锁等待过期、
最小角色、真实提交后丢失回执；实际 PG 升级验证 DDL/版本回滚和旧记录/ACL 保留。
原生 B/SQLite 与真实回环 TLS 验证正常 POST ACK、接受后丢失响应只 GET、旧事件404不补发，
以及先接受 STOP 后拒绝迟到 prepare。PG 子进程用实际安装入口验证 ENV 装配、单次运行、
SIGTERM 不发送 STOP 和 owner 连接在访问控制器前被拒绝。

上述 daemon、批准计划和 readiness 均为合成事实，不加载模型，不证明 GPU、网络资格、
生产 TLS 或多主机故障资格。投递 ACK 不调用 `observe_runtime`，也不推进 peak→steady 或
释放基线；观察/预算投影仍需既有窄事务及真实可信证据。B 节点内观察生产者、A 的批准计划/
真实资格 provider、完整调度与业务消费者仍待接入；本单元不关闭 A01/A03 或整个 Gate。
