# Runtime 意图存储与未观察快照

属于 [A01 #20](https://github.com/hrygo/SpeechNexus/issues/20)。本单元提供
`0005_runtime_intents`、最小列授权、协调快照契约，以及独立的 PG
`create_runtime_intent` / `observe_runtime` / `query_outcome(runtime_intent)` 事务端口。
`0006_runtime_observation` 保存私有 readiness 证据；0001–0005 迁移保持不变。
**批准计划/资格提供者、调度循环和控制器客户端尚未接入运行进程。**
端口验证不代表完整配方准入、
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
create 端口将完整冻结意图、预算与事件同事务写入；后续派发必须在提交后调用控制器，
未知时查询原 node/intent，不能创建新 generation 或重新预留。

## 时间、旧记录与升级

runtime_instance_intents 增加协调 recorded_at_us / updated_at_us 和私有
observation_document。时间须成对存在、位于安全整数范围，且更新时间不早于创建。
recorded_at_us 不授予运行角色 UPDATE；观察正文与更新时间仍属于 A 的受控写入，
gateway 的窄 SELECT 不包含正文。

迁移不为历史意图填入当前时间或编造观察。历史缺失记录保留三个 NULL，
生产读取/恢复端口将它们识别为 reconciliation_required，不能普通重放或派发；
受控 adoption 和旧 writer 隔离跟随 A07 交付。数据库不独立验证 JSON 语义或
跨表资源规则；这些由领域/应用事务执行，未接入的状态变更仍不得绕过规则。

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


## 独立 create/query 事务端口

`RuntimeService` 使用窄 `RuntimeTransactions` 端口。PG 适配器使用显式非 owner
`speech_pool`；内部 `RuntimeTransactionBoundary` 接收组合根注入的机器契约 codec
及服务端 owner_id，不接收客户端声明的 owner。当前没有新 HTTP 路由、默认批准
计划或自动启动的控制循环，不能仅凭一个合法请求自行创建任意容器。

prepare 要求服务端显式提供 `ApprovedRuntimePlan`：绑定、模板、manifest、配置
摘要、拓扑与资格摘要、loading policy、slots、完整六维 peak/steady/per-slot。
构造对象本身不证明模板获准或 GPU 资格；组合方必须从部署批准的权威来源取得这些
不可变事实。catalog/资格提供者和实际进程装配继续待交付，不允许用客户端计划或
当前可变默认值填补。stop 使用原 generation 保存的计划，不从新默认值重建。

事务先只读定位，再按池、host、head、instance、intent、lease、gateway barrier、
submit 的全局顺序锁定并重新核对作用域；跨节点/池替换会排序锁住旧、新父记录。
锁等待结束后取 DB 时间，核对 owner_id、fencing、租约和请求期限。prepare 同一
事务增加 host 全六维 held peak，建立 preparing generation/head、完整意图和 runtime
outbox。隔离预算始终计入容量，不得因 TTL 或取消归还。资源计数限制在安全整数范围，
版本使用契约规定的字符串表示，unified memory 不再重复计 host/device RAM。

私有 request_document 使用受控 `record_version: 1` envelope，保存完整控制器请求、
creator_id 和完整批准计划。请求摘要基于完整规范化 wire JSON，保留原 deadline
文本的全部精度；事件身份固定原 node/intent。重放/查询核对正文摘要、不可变实例
配置和 outbox 目标，不分配新预算，不重新解释批准计划。缺少完整 envelope、时间或
相关证据的旧记录进入协调；不自动 adoption。

stop 只将实例标为 draining 并记录原命令，不归还 host、base 或 slot 预算。
普通 stop 要求没有 held lease、活动 submit 或未结束的 runtime intent。
接管清理仅允许旧 fencing 的 quarantined lease，且必须已经有 B 的持久 sealed barrier
覆盖当前 fencing；unknown_quarantined submit 不再活跃执行提交，但其物理预算仍保留。
这些前提不证明引擎已停止。替换 generation 还要求原实例 stopped、base released、全部
lease 已释放、原 stop 观察含退出与资源释放证据，且新 generation 大于历史高水位。
既有 preparing/reconciling/quarantined 未决意图继续阻止普通新 stop/replacement，须经
后续观察/协调路径处理。基础预算标签为 peak/steady/q_peak/q_steady/released，保留
隔离前的资源依据；下述独立 observe 端口受控执行观察驱动的基线预算转换。

PG 每个语句受剩余请求期限约束，提交确认后才返回 committed；仅确认死锁或序列化
回滚后有限重试，抖动发生在释放连接之后。唯一冲突回滚后只读查询原身份；连接或
提交确认中断返回 unknown/query_original，不自动再次写入。查询缺失也保持 unknown，
不能据此假定前次未提交。无推理、HTTP 或对象写入占用数据库事务。

新增双库测试使用相同生产 SQL/领域规则与非 owner 角色，覆盖共享容量竞争、跨主机
同一 generation 竞争、末尾 outbox 失败整笔回滚、原身份冲突、锁后期限、停止不释放、
隔离清理屏障及原记录损坏。真实 PG 提交后人为丢失确认，验证只读恢复且不重复写入。
测试中的观察与释放事实由明确的合成 fixture 提供，不是 observe_runtime 的实现证据，
也不证明真实 GPU、容器、模型或多主机资格。


## 独立 observe_runtime 端口

`RuntimeService.observe`、PG 适配器和内部契约边界接收完整实际控制器响应、期望
instance/intent version，以及显式服务端 owner。现有 create/query 组合可继续使用；
启用 observe 必须装配 `observation_for_boundary`，缺少 codec 时拒绝观察，不产生
默认成功。没有新增 HTTP 路由或真实 controller/warmup 客户端。

按池→host→head→instance→intent→lease→submit 的全局顺序锁定，核对原冻结请求的
全部身份、memory、generation/action/fencing、当前 head 指针、版本及 owner 租约；
锁等待结束后取 DB 时间。事实观察允许发生在原请求 deadline 后，因为记录既有事实
不等于再次执行。旧 generation 的新观察不能修改当前 generation。

| 实际观察与前提 | A 的动作 | 持续限制 |
| --- | --- | --- |
| preparing/loading/warming_up | 保存原观察与完整 peak | 不提前归还 transient budget |
| ready 且受信 gateway warmup/资格 receipt 有效 | 原子 peak→steady、写首次 ready_at | 当前 owner 必须 active；receipt 与实例、绑定、拓扑、qualification、fencing 一致 |
| failed、没有停止释放证据 | 标记 failed，保留原 peak/steady | 失败不代表资源消失 |
| quarantined、旧 owner epoch 或观测超限 | 原已保留 base 转入隔离、保留 peak/steady 依据 | 旧 epoch 的实际终态可结束原 intent，便于当前 fencing 清理；不得恢复 ready |
| stop 成功但 lease/submit 清理尚未完成 | 保存原停止事实，intent 为 reconciling，base 继续占用 | 不写 stopped_at、不归还 base，不允许后继 generation |
| stop 成功、全部 lease released、submit 正确终结且其他 runtime intent 终态 | 从正确 held/quarantine 分量扣除原 base，写首次 stopped_at | 不扣减其他实例或 slot 预算；host 不自动恢复 active |
| 同一已记录事实重放 | 返回原完整事实，不重复变更预算或时间 | POST replay flag/等价可选 null 不构成新事实 |

ready 的 `VerifiedRuntimeReadiness` 是明确受信 provider 的私有输入，包含绑定、
instance/generation、fencing、拓扑与资格摘要、evidence_digest、核验/失效时间。
构造对象或合法摘要本身不证明真实 warmup/资格；实际 provider 与批准来源仍待交付。
缺少或过期/错绑定的 receipt 时保留 peak，不能由公开请求自称 qualified。
旧 epoch 或已经隔离的 generation 即使收到真实 ready，也只记录终态并保留隔离，
不能凭后续成功响应自动恢复接单。实际准入以 A-owned instance/host/lease 权威为准，
不能只凭 `RuntimeSnapshot.runtime` 中控制器的 ready/succeeded 判定可调度或资源已返还。

`0006_runtime_observation` 只增加私有 readiness_document 及基本形状约束，保存
受控 record_version=1 的完整 receipt，不给历史 NULL 补造资格。A 的授权生成器增加
该列 UPDATE，B 的窄 SELECT 继续排除正文；迁移不自动扩大既有角色的表/列权限。
部署 owner 须审阅并安装更新后的授权输出，运行进程不执行 DDL/GRANT。
首次 receipt 与实际 ready 观察、基线变化同事务写入；原 ready_at 和证据在后续停止
中保留。旧迁移与 Job/outbox、历史命令和原 ACL 保持，破坏性降级继续拒绝。

控制器的 created/updated/verified 时间原文保留，和 A 的 DB recorded/updated/ready/
stopped 时间分别表达。比较保留亚微秒部分，不向下截断未来停止证据来提前返预算。
原创建时间、已知 peak 高水位、停止证据与终态事实不可改写；新增进度不能倒退。
观测超限时隔离已保留量、保存完整测量并禁用 host；它不证明实际 GPU 上界得到强制执行。
任一维隔离比例超过配置阈值时，host 在同一事务离开 active；释放后或比例下降不会
自动恢复，需重新健康/拓扑/入口资格。新状态变化不制造 outbox 外发时间，派发器后续
必须查询原 node/intent 和已存观察，不能把未确认通知当普通重发许可。

真实停止事实可以先于独立 lease 释放流程入库，供后续核验；等待阶段不返还 base。
lease/submit 清理完成后，使用当前 instance/intent version 再观察同一不可改写事实，
才关闭 A 的 reconciling 并一次归还基线。unknown_quarantined submit 即使配上声称
released 的遗留 lease 也继续阻止返还。当前测试中的 lease 清理是显式合成 fixture，
不是 quarantine_lease/release_lease、B 真实停止或全链路恢复的交付证据。

PG/InnoDB 使用相同 SQL/领域规则和非 owner 角色验证；覆盖真实锁等待后的过期、
并发观察版本竞争、同事实重放、完整六维转换、迟到 epoch、隔离不自动恢复、
未知 submit、停止后延迟返还、事务末尾失败整体回滚、真实提交后确认丢失，以及失败
升级/重复升级/原行与列 ACL 保留。资格 receipt 和控制器响应均为合成事实，不代表
GPU、真实模型、物理多主机、容器上界或已运行的 scheduler。ADR0007 的旧路径退出
仍需逐项映射和真实消费者证据，本单元继续保留旧门禁。
