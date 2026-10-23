# Runtime 意图存储与未观察快照

属于 [A01 #20](https://github.com/hrygo/SpeechNexus/issues/20)。本单元提供
`0005_runtime_intents`、最小列授权和协调快照契约，作为后续
`create_runtime_intent` / `observe_runtime` / 持久派发的准备。
**这些生产事务、调度循环及控制器客户端尚未接入。** 存储正负例不代表实际准入、
物理停止、GPU 或多主机资格；实施顺序见 [runtime 单元计划](../plans/2026-10-24-a01-runtime-intent-unit.md)。
兼容边界和消费者顺序由 [ADR 0008](../decisions/0008-runtime-intents-before-controller-observation.md)登记。

## 意图与观察分别表达

协调者先记录意图，控制器收到后才产生自己的观察。协调数据库的记录时间不能
冒充控制器创建/更新时间，也不能在没有观察时构造 `absent`、`ready` 或 `stopped`。

`RuntimeSnapshot` 保留既有观察分支，新增无观察分支：

| 情况 | `runtime` | 事实来源 |
| --- | --- | --- |
| 尚无控制器观察 | `null` | 冻结 request、`intent_state`、`recorded_at_us`、`updated_at_us`、version 来自协调记录 |
| 已有控制器观察 | 完整 `RuntimeInstanceIntentResponse` | 保留控制器响应，包括其创建/更新时间与原停止证据 |

无观察分支只允许 pending/reconciling/quarantined，不能报告 succeeded；
`observe_runtime` 的成功结果必须使用观察分支。提交了意图不等于完成了生命周期动作，
查询到 `runtime: null` 不授予 ready/停止/释放权限，未知预算仍须保留。
控制器 HTTP 的请求/响应与观察状态枚举未改变；新增形状只属于协调事务信封。
协调消费者需先处理 nullable runtime，再接入新意图生产者。该内部契约随集成 SHA
交付，不能只凭旧生成包或相同版本字符串假定已经具备新分支。

`contracts/bindings.json` 的协调 bundle 现可离线校验 create/observe 请求及完整
TransactionResult。共享正负例位于
`tests/fixtures/coordination/transaction-examples.json`，包含未观察创建/查询与拒绝
伪造完成/观察的回执。不存在新增的公开业务 HTTP 路由。

## 持久 outbox 的目标

旧 outbox 的 job_id 必填，不能用于控制器意图。新迁移增加判别与节点作用域外键，
原 Job 外键和事件身份保留：

| `event_kind` | `job_id` | `target_node_id` / `intent_id` |
| --- | --- | --- |
| `job`（旧事件和旧写入的默认值） | 必填，引用原 Job | 必须均为 NULL |
| `runtime` | 必须为 NULL | 两者必填，引用同一节点作用域的既有 runtime intent |

CHECK 拒绝未知 kind、混合目标和半个 runtime 身份；外键拒绝不存在或其他节点的
intent。新索引支持按 kind/未投递状态/事件身份查询。记录不可被运行角色改换目标；
仅 delivered_at_us 可按既有所有权更新。
这只提供原子写入和后续轮询所需的存储形状，尚无生产派发进程；不宣称恰好一次外发。
未来事务必须将完整冻结意图、预算与事件同事务写入，提交后才调用控制器，
未知时查询原 node/intent，不能创建新 generation 或重新预留。

## 时间、旧记录与升级

runtime_instance_intents 增加协调 recorded_at_us / updated_at_us 和私有
observation_document。时间须成对存在、位于安全整数范围，且更新时间不早于创建。
recorded_at_us 不授予运行角色 UPDATE；观察正文与更新时间仍属于 A 的受控写入，
gateway 的窄 SELECT 不包含正文。

迁移不为历史意图填入当前时间或编造观察。历史缺失记录保留三个 NULL，
后续生产读取/恢复端口必须将它们识别为待核对历史，不能普通重放或派发；
受控 adoption 和旧 writer 隔离跟随 A07 交付。数据库不独立验证 JSON 语义或
跨表资源规则；这些仍须由即将接入的领域/应用事务执行。

使用独立 owner 凭据运行既有迁移 CLI 到 head，再以 owner 审阅并安装
`coordination_runtime_grants` 的新输出；API/worker 不执行 DDL 或 GRANT。
迁移保留旧 Job/outbox、原请求和表/列授权，不自动扩大既有运行角色授权。
新 runtime 事件需要额外的 outbox INSERT 列授权；只持旧创建授权的角色不能写它们。
禁止破坏性 downgrade，以免丢弃原意图、观察或派发历史。

引入 runtime 事件前，先升级按 event_kind 选择目标的消费者。迁移本身不生成
runtime 事件，也不启动任何控制器/引擎。现有 Job 创建、幂等检查和 Asset 路径继续
使用原身份与协议；0001–0004 的已发布 DDL 保持不变。

## 验证与退出复核

针对性证据包括 PG 包内迁移及 InnoDB 对同一新增约束的 oracle：
目标判别/外键、时间负例、非 owner 权限、原 Job 重放、失败升级整体回滚、
重复升级和原表/列授权保留。InnoDB 只在测试中转换必要的 DDL 语法和字符序，
不增加产品 MySQL 支持或独立领域规则。

```sh
uv run pytest tests/contract/test_runtime_pending_snapshot.py -q
# 必须配置项目隔离 PG/InnoDB 和集成所需 MinIO。
make coordination-test
make integration-test
```

ADR 0007：本单元补入协议/持久化约束，尚未迁入 runtime 状态、预算、停止与
恢复的完整生产行为，旧机制的退出条件未满足，继续保留相关门禁。
