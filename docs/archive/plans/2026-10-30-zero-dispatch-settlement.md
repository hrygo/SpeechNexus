---
title: 静音零外发结算的固定边界与后续输入
status: partial-implementation
date: 2026-10-30
---

# 静音零外发结算：A 侧补强与未启用边界

> 本文记录固定基线及候选方案的交接证据；文中“当前”“已实施”和部署状态均以各段受检版本为限，不能据此判断现行代码或部署。后续采用前须对照当前契约、实现及[交付跟踪矩阵](../../plans/delivery-tracking.md)重新核验。

DEL-361 基线为 `a1ad444db343d76699e83b7f93d8a93355db7b3c`。输入是 DEL-357
`interface-candidate` revision `97b863c3-9c5d-405f-8eee-519fc06c338f` 与 DEL-358
`review` revision `025f6eed-6c73-4916-a81c-d93bb37978ea`。
本文件中“后续协议”是待 A/B 实装并经非作者 Sol 审查的设计，**不是现行机器协议**。
CPU VAD 无语音返回空正文/no_speech 的产品语义继续有效。

远端 main 已通过只读 get_commit(main) 核对为上述基线。DEL-356 正式交接
`del-356-binding-head-handoff` revision `25fdd6d4-53c6-414c-984d-d591d68003ed`
记录同一 base 上 25 文件未提交候选，patch 122598 bytes、SHA-256
`4b91ac055f6fff01cf1065b7fe31b902dc7c86e1e4ed7a076c65a4670217fdc1`。
这里读取的是正式交接，未读取其作者工作树或取得/应用该 patch；未将其 pin/deadline
修复当成 main。DEL-361 只基于 main，未叠加 DEL-356；两个候选都改契约 inventory，
后续集成须由唯一写者重新生成，不能直接覆盖生成后像。

## 当前已实施的窄边界

`release_lease` 的既有 `failed/not_required` 分支以前只核状态，未自行复核接受/派发/
结果历史。故障写者留下 `engine_request_id=file-asr-*` 或 dispatched_at、staging、
生成样本后再写 `failed/not_required`，A 原实现仍可能结算。此处是消费者对不一致
存储的防御缺口，不声称正常 B 端口曾合法生成这种状态。

本次在原 submit 行锁后读取 `execution_release_guards`：只投影 submit ID 与一个
0/1 的结构一致性条件，核对原 request ID、dispatched_at、staging key/receipt 均为空、
generated samples 为零且没有任何 output interval。缺投影、缺权限、缺行、非法值或
历史存在时拒绝，不改预算。该投影是现存 B 失败事实的附加拒绝条件，**不是停止事实**；
默认值为拒绝，外部 release 请求不能传入这个字段。

迁移 `0028_execution_release_guards` 接在 main 的 `0027_gateway_abort_intents` 后；
仅增加只读 view，不改 B 表/worker、身份或历史。两套 A 结算授权函数均增加该 view
的 `SELECT (submit_intent_id, no_dispatch_metadata_absent)`，
不授予 A 私有命令、vendor ID、对象路径、结果正文读取，也不授予新证据写权。
**升级必须先按下节停用、排空并栅栏全部旧释放者，再迁移/授权、升级和恢复新消费者；
不能滚动混跑。** 启动过程不执行 DDL。缺安装条件时事务拒绝。旧 A 消费者仍有旧缺口，
回滚消费代码不能作为安全回滚；必须回滚时仍先执行同样的停用/排空/身份栅栏。
单独 downgrade view 会让新消费者拒绝结算；不会解除占用或删除历史。

未新增零外发 evidence 枚举、成功转换、生产开关或假 producer；既有真实停止路径与
从未提交的 lease 路径保留。只有执行增量属于此 lease；运行中实例的 baseline 不能
通过任务完成退还。此改动不能阻止恶意 owner 擦除历史，也不能封锁持有旧引擎凭据的
外发者。它不能结算合法静音，不能代替下面的 P1/P2 前置。

## DEL-366 返修：worker 权限与释放消费者切换

输入为 DEL-366 `review` revision `767764e1-65cd-4c56-8359-958634462d00` 和 DEL-361
`independent-review-handoff` revision `78866b4e-de36-4e53-a3f1-41967c4c0406`。
旧候选 patch revision `5fbb4541-613a-4186-8b99-4ca846f0415f`、manifest revision
`ab6591ca-bb76-46bc-a6d4-7adfe8b87cc1` 保留，不覆盖；本次形成新的完整候选与增量。

### P1：正式 worker 启动权限

`binding_disable_worker._verify_settlement_principal` 只允许 presence view 的上述两列读取；
缺 view、缺任一列权限、额外私有列读取、证据写权及 view 写权都在启动时拒绝。
也拒绝 view 的**表级 SELECT**（包括 PUBLIC/继承所得），避免未来追加列自动暴露。
两套授权函数只声明列授权，不自动撤销管理员已有授权；若试装过旧候选的表级 SELECT，
migration owner 必须在消费者停用窗口显式撤销该授权及其继承/PUBLIC 来源，再按新声明授权。
不能靠删除启动校验或回收新 view 的必需列授权修复启动失败。

正式 worker 的扫描身份仍无 view/预算写权，与 settlement 身份分离；两者必须连接同一数据库。
启动校验失败时不构建 gateway 客户端、不进入循环。缺 view 时既有有停止证据的直接 lease
端口仍可独立工作，但本 worker 启动需要完整依赖；两者的可用性声明不能互代。

### DEL-370 P2 返修：专用身份禁止额外 SET ROLE

DEL-370 `review` revision `1e44accf-001e-4fd6-b63c-9cf36f096dc9` 指出 v2 只检查
连接时的 login/effective ACL，无法发现非 owner 角色的 `INHERIT FALSE, SET TRUE` 权限。
DEL-372 在专用隔离 PostgreSQL 16.15 上先复现直接/间接两类路径：初始读取私有列失败，
同一连接 SET ROLE 后可读；v2 仍通过检查并构造 gateway。源私有列和 view 表级读取各一例。

本 worker 的 scanner 与 settlement 都是专用身份，不需要切换成其他角色。启动时先要求
**PostgreSQL >= 16**，再用 `pg_catalog.pg_has_role(SESSION_USER, role_oid, 'SET')`
检查所有非自身角色；任一可达就拒绝，即使该目标当前没有权限。不枚举目标的“安全 ACL”
作为例外，避免新增列、后续授权和仅 SET 后生效的角色属性扩大凭据权限。
PostgreSQL 原生函数处理直接和递归路径，每条边须 SET TRUE；SET FALSE 截断路径。
数据库拒绝循环成员关系，测试实际提交循环 GRANT 并核对拒绝；实现不自行递归或修改系统目录。
依据：[PG16 角色成员](https://www.postgresql.org/docs/16/role-membership.html)、
[原生权限查询](https://www.postgresql.org/docs/16/functions-info.html#FUNCTIONS-INFO-ACCESS-TABLE)。

`INHERIT TRUE, SET FALSE, ADMIN FALSE` 可提供原有精确最小 ACL，正常 worker 仍启动和结算；
继承或 PUBLIC 导致的额外列/表权限继续由原有效 ACL 检查拒绝。非继承且 SET FALSE 的不可达
成员路径不因其他非 owner 角色的私有 ACL 被误判为有效权限。既有数据库/表 owner 的 MEMBER
拒绝保持保守，不因 SET FALSE 放宽。登录/effective 身份的 SUPERUSER、CREATEROLE、CREATEDB、
REPLICATION、BYPASSRLS 均拒绝；任一其他角色的 ADMIN 权也拒绝，防止凭据自行重新授予 SET。
ADMIN 判定同样使用原生 `pg_has_role(..., 'MEMBER WITH ADMIN OPTION')`。

版本未知/低于 16、权限结果缺失或非明确 false、目录查询失败/无权/超时/连接中断均不能启动；
错误向组合根传播，在构造 gateway、创建 coordinator 和进入 worker 循环前终止。入口仍只输出
固定失败消息，不回显角色名、连接串或数据库异常。本次无机器契约、授权生成器或迁移增量。

这是启动时数据库实际角色/ACL 的检查，不是阻止管理员后来改授权的数据库策略；运行期间改变
角色/ACL 必须沿用停用、排空、核验后重启的部署窗口。它不代替下节生产身份清单、旧释放者
切换/事务排空证据，不修复原正向新门的 P1/P2，也未实现正向静音成功。

### DEL-415 P1 返修：拒绝有效继承的预定义能力

DEL-372 正式独立复审 `c5674096-d2a1-4324-8579-21a1ebd66c8e` 在隔离 PG16 实证：
已有精确最小 ACL 的 settlement 身份若被部署管理员授予服务器文件读取或程序执行能力，
即使 SET/ADMIN 为 false，仍可通过 v3 启动检查。该发现依赖已有授权，v3 未授予这些能力。

两个专用身份都不需要 PostgreSQL 预定义能力。DEL-415 在同一启动目录查询中，对保留前缀
`pg_` 的全部角色，检查 SESSION_USER 和 CURRENT_USER 的原生
`pg_catalog.pg_has_role(..., 'USAGE')`；任一有效能力均拒绝。该函数判断无需 SET ROLE
即可使用的直接/传递继承，INHERIT FALSE 会截断，不能用 MEMBER 或只检查直接成员替代。
目录中出现的新增预定义角色沿用拒绝策略，不用固定三个角色名作为完整允许边界。

| 预定义能力 | 专用 scanner / settlement 策略 |
| --- | --- |
| `pg_read_server_files`、`pg_write_server_files`、`pg_execute_server_program` | 拒绝有效 USAGE，包括 SET/ADMIN false 的传递路径 |
| `pg_read_all_data`、`pg_write_all_data` | 拒绝；全局数据能力超出精确 ACL |
| `pg_monitor`、`pg_read_all_settings`、`pg_read_all_stats`、`pg_stat_scan_tables` | 拒绝；监控配置、统计或扫描由独立运维身份承担 |
| `pg_signal_backend`、`pg_checkpoint`、`pg_use_reserved_connections`、`pg_create_subscription` | 拒绝；专用 worker 无信号、维护、连接保留或复制订阅职责 |
| `pg_database_owner` | 拒绝隐式有效成员身份，保留原数据库/表 owner 检查 |
| 较新服务器提供的其他 `pg_` 角色（如维护、autovacuum 信号） | 同样拒绝；不据此声称该版本已实际验证 |

普通 ACL 分组的 `INHERIT TRUE, SET FALSE, ADMIN FALSE` 仍允许；仅 MEMBER 可达而
USAGE/SET/ADMIN 均 false 的服务器角色不误判为有效能力。既有 SET/ADMIN、owner、角色属性
和精确表列 ACL 检查继续执行。新增能力结果缺失、NULL、非明确 false 或目录查询错误均拒绝；
错误在 gateway 构造前传播，不执行服务器文件/程序探测来尝试授权。

依据：[PG16 预定义角色](https://www.postgresql.org/docs/16/predefined-roles.html)、
[USAGE 与 SET 的原生定义](https://www.postgresql.org/docs/16/functions-info.html#FUNCTIONS-INFO-ACCESS-TABLE)。
完整复现与验证原件随 DEL-415 固定候选交付；启动快照、部署授权变更窗口和下节旧释放者切换
要求继续有效。本返修不改变失败结算协议、授权生成器、迁移或正向静音的未完成状态。

### P2：先封闭旧释放者，再声明防护生效

当前仓库的释放调用链如下。以源码及 `rg` 核对，没有可用 codebase-memory 索引；
这里不是生产部署进程清单。部署负责人还须提供所有实际实例、旧制品、进程/任务及数据库身份映射，
覆盖直接使用窄端口的自定义消费者和仍持有效凭据的离线/延迟任务；清单不完整就不能恢复释放。

| 仓库入口 | 原调用链/身份范围 | 切换要求 |
| --- | --- | --- |
| `binding_disable_worker.run` | `BindingDisableCoordinator.run_pass` → `LeaseService.release` → `PostgresLeaseTransactions.release_lease` | 停止全部旧 worker；同时排空扫描、abort 和结算；独立 scanner/settlement 身份轮换 |
| `LeaseTransactionBoundary.release_lease` | 契约边界 → 相同 service/adapter；当前不是 HTTP 路由 | 暂停所有实际宿主和调用队列，不能只停 Web API；覆盖 `coordination_runtime_grants` 的 A 身份 |
| `LeaseService.release` / adapter 直接调用 | Scheduler/reconciler 或自定义组合根；本仓库除上述 worker 外未提供另一运行中的调度组合根 | 逐项登记实际消费者，冻结原请求/lease 身份；覆盖两套 grants 的全部实际授予者 |
| 共享凭据、继承角色或人工 SQL | 绕过应用的数据库写能力；不属于正常释放端口 | 由数据库 owner 核对有效写权及登录角色闭包；旧运行角色不得仍能写预算/lease |

以下是**待部署负责人获准后执行**的停止切换顺序，本任务未操作生产配置/服务。

1. 固定新候选/迁移/授权字节和部署清单。暂停全部释放入口及队列，禁止调度器重启旧进程。
   暂停期间允许按原身份只读查询；不据超时将任务改失败，不改 lease、预算、历史或实例 baseline。
2. 数据库 owner 对清单内全部旧登录角色关闭新连接（例如 `ALTER ROLE ... NOLOGIN`），
   禁止旧角色的 SET ROLE/继承路径及共享凭据被其他登录者重用。**NOLOGIN 不终止已有连接**。
   有界等待既有事务结束；不能确认排空时终止明确属于旧角色的会话，等待实际回滚和会话归零。
   不能只看容器退出、连接池关闭或 `idle`；包含 `idle in transaction`、锁等待及预备事务。
   如有 prepared transaction，先按原身份协调到确定 commit/rollback，禁止直接恢复新消费者。
3. 撤销旧运行角色对结算表/列的有效写权，含表级和列级授权、PUBLIC/继承来源、可切换身份。
   单独 `REVOKE UPDATE ON table` 不撤销既有 `UPDATE(column)`。只撤销运行权限，不 DROP OWNED、
   不删源行/stop/interval/账目。owner 身份不交给运行进程。再次证明旧身份无新登录、无存活事务、
   无可写角色路径；有提交结果未知的原请求先只读查询，不新建执行或自动重放旧写事务。
4. 在旧写者被排除后，由独立 migration owner 应用 0028；显式移除旧 view 表级 SELECT，
   为**新专用凭据/角色**执行新版两套最小 grants，不把新凭据发给旧制品。
   若旧新共用身份、不能确认旧任务不再运行或不能隔离凭据，此处停止，保留预算，不能滚动更新。
5. 部署新字节，验证 view owner/来源、列权限和角色继承/PUBLIC 权限，再启动正式 worker，
   观测其真实进入循环；缺依赖/过度授权启动拒绝。用固定的合成反例确认新端口拒绝
   `failed/not_required` 与接受/结果历史矛盾的记录，全部账目和源历史不变。
   无实际部署回执前只报告候选已验证，不称现网保护已生效。
6. 只恢复清单中逐项核过的新消费者。旧事务若在停用前已经 commit，不能凭此次升级倒扣或补造 stop，
   先按原身份查询并交协调处置。回滚仍先重复步骤 1–3，保持释放暂停；不能给旧代码重新授予写权。
   view downgrade 保留全部来源，仍不能授权旧消费者恢复写入。

切换证据至少包括：固定制品、每个实际释放者与登录/effective role 的映射、停用回执、
旧登录/成员路径与写权限撤销、存活/锁等待/预备事务归零、未知结果原身份查询、
新权限检查和真实循环回执、合成拒绝前后账目/历史对照。生产部署清单和这些部署回执目前均未提供。
这套切换仅封闭 A 的旧结算者；后文 B 的旧引擎外发者/网络凭据栅栏仍是独立前置。

### 已实际运行的返修验证边界

隔离 PostgreSQL 16.15 上先运行原正式 worker 组合根，实际复现 P1 启动拒绝。
修复后执行 `tests/integration/test_disable_composition_root_pg.py`、
`test_zero_dispatch_worker_pg.py` 和 `test_zero_dispatch_release_pg.py`：真实受限双角色启动、
loopback HTTP 的真实 gateway 组合根原 abort/restart、真实 worker 结算循环和权限反例。
worker 对一致的旧失败事实只释放本 lease 增量，保留合成实例 baseline；接受历史矛盾则 pending、
账目和源事实不变。引擎适配器和 lifecycle 行为中的合成事实不证明物理停止。

另外执行一次性旧/新对照探针：从完整 base 精确提取并核 hash 的两份旧 release 源码，
在独立子进程中跑 port/service/contract boundary 三入口。未栅栏时三者均实际错误 committed；
另一组先观测旧事务的真实 pool 锁等待，NOLOGIN 后旧会话仍存在，终止会话使旧事务 unknown/回滚，
撤销表级及列级写权，验证旧登录和有效身份写入被拒；再执行新消费者，三者均拒绝矛盾历史且
全部源事实/账目不变。共 6 个对照，不是仅用新规则模拟旧实现。
探针和原始日志随新候选交接；它依赖固定 base Git 对象，作为历史对照材料，不加入永久产品测试。
这证明测试隔离身份的切换顺序，**不证明生产所有释放者已排空/栅栏**，也不证明 B 外发栅栏、
真实资源停止、MinIO、双库、多机、HA 或完整静音成功 AC。

## 后续协议的原身份与状态矩阵

新事实必须绑定原 submit/lease、operation/attempt/stage/workspace/pool、node/gateway、
instance/generation、binding/policy/voice、fencing、command 与 request digest、完整固定
request_document 的受控引用、原 worker engine_request_id/dispatched_at；摘要不替代正文。
原 worker 接受身份必须保留，不能清空后借 `failed/not_required` 释放。

| 持久事实 | 允许的新动作 | A 结算/公开结果 |
| --- | --- | --- |
| 旧执行、缺门、缺可信 epoch 或有未协调旧外发者 | 查询原身份/既有停止恢复 | 保留占用；禁止补建 open 或零外发成功 |
| 新合格执行 open，无取消、无许可 | 可竞争取得一次 permit，或完成处理后 seal | open 本身不构成释放证据 |
| permit 已提交或提交结果未知 | 查询原 permit；禁止零外发 seal | 按现行引擎/实例停止规则；不因“没看见 HTTP”释放 |
| 取消权威提交先于 seal | 禁止 permit/seal；异步关闭 gate 仅为投影 | 取消不是停止；等待可信增量资源关闭事实 |
| processing_closed + 固定对象版本验证 + open，无取消 | 可原子写 sealed_zero 与不可变终结事实 | 仅在完整新消费者和资格通过后允许 succeeded/no_speech |
| sealed_zero 已先提交 | 原意图查询/重放；拒绝迟到许可与改意图 | 原事实不反转为取消；结果发布与 lease 结算分别幂等 |
| 输入不完整、VAD/关闭失败、子任务仍在、PUT/version 未知 | 保存原意图、隔离协调 | 无零外发成功、无新身份重试、无释放/发布 |
| engine_possible（即使识别文本为空） | 现行可信引擎终止路径 | 不能转换为 sealed_zero |

后续封闭事实不复用 engine_terminal_response，也不伪称整实例已停止。取消先完成的
执行将来可以有独立的零外发关闭事实，但终态必须是 aborted；资源证据不完整时仍
pending/quarantined。本次没有为该分支增加机器枚举或消费者。

## 取消线性化与完整锁图

新设计选择**关系数据库中原 submit 对应的不可逆取消意图提交**作为唯一取消线性化点，
不采用 claim_abort、HTTP 连接取消或本地标志。A 控制面取消与 B receive_abort 必须
汇入同一个原身份事务端口；先持共同父权威锁和 submit 锁，再写不可逆取消事实。
新 seal/permit 在相同锁下重读它，不能只锁可能不存在的 abort 行。
取消接收回执仅可在该事务提交后称为已生效；更早的跨进程投递只能称 pending。
这需要 B 修改 receive/claim/非 durable abort，不能由 A 在释放时事后修补。

采用现行全局顺序的子序列（见 coordination-transactions 第4节），扩展如下。
所有多键先完整收集并同类排序，不可按 stage 走完整链；FK 隐式锁同样受约束。

```text
新准入独有的 egress-epoch authority（若需锁，必须在全部资源锁前）
→ 既有 binding advisory barrier（只在本来需要绑定裁决的入口）
→ resource_pools（全部排序）
→ workspace_quotas
→ host_nodes（全部排序）
→ runtime_instance_heads
→ engine_instances
→ runtime_instance_intents
→ admission_operations
→ admission_attempts
→ admission_requests
→ admission_leases
→ gateway_fencing_barriers
→ submit_intents
→ 新 gate / 原身份取消权威行（先有 submit 父行，再读/插入缺失行）
→ gateway_abort_intents
→ output_stream_heads
→ 不可变处理/封闭/停止证据、output_sample_intervals
→ coordination_jobs
→ coordination_outbox
```

| 入口 | 已读 main 锁链与必须保持的关系 |
| --- | --- |
| A release/quarantine | pool → quota → host → instance → attempt → lease → submit；新 view 是锁后的普通读取，不增反向锁 |
| B create_submit | 可选 binding barrier → pool → head → instance → attempt → lease → submit/stream |
| B record_stop/记录观察/恢复 | lock_submit_intent：pool → 可选 lease → submit；不得持 submit 后补 head/instance |
| B receive_abort/claim_abort | pool → submit → abort；现行接收和 claim 分事务，新规则必须在接收提交处桥接权威取消 |
| A runtime prepare/stop | 全部 pool → 全部 host → head → instance → runtime intents → leases/barriers/submits 的适用子链 |
| 新 permit/seal/cancel | 若需当前 generation/资格裁决，先 pool → head → instance → attempt/lease，再 submit → gate/cancel；不能先 gate 再取早序锁 |
| 新 A release_zero | 现行 A release 前缀 → submit → 固定 gate/证据读取；不得回取 epoch/head；消费封闭时已固定的资格事实，不用最新默认值重新解释原结果 |

新 epoch 的关闭/轮换只能停新准入，不能改写历史证据。涉及多个 pool 的控制/停止必须
先锁全体 pool。任何新端口不满足上述子序列时必须先调整实现，再冻结机器契约；
现行 pool 串行不能被当成任意锁反转的许可。死锁/序列化失败仅在 DB 确认整笔回滚
后按原意图有界重试；COMMIT 未知只查询原事实，不换 submit、key 或 generation。

## P1：外发栅栏、受限 producer 与权限

可接受资格输入必须来自实际部署权威，固定 epoch、gateway build/装配摘要、目标
instance/generation、credential/network enforcement 的不可变证据引用和覆盖的传输入口。
版本字符串或 `qualified=true` 不能签发资格。B 必须清点 command_dispatch、file ASR/TTS、
最终 vLLM 传输、恢复和迟到任务；所有持旧命令/凭据的进程、入口和直接客户端均被
撤销凭据或网络拒绝后，才可接受新 epoch。单纯排空而未撤销旧外发能力仍不足。
本任务不修改运行配置或实际执行此切换；当前没有这种部署回执，能力保持关闭。

处理回执只允许来自 B 指定的受限处理 supervisor。必须固定输入对象版本、摘要、
长度、解码 EOF/样本钟、VAD 模型/策略版本、全部段完成、媒体和对象句柄成功关闭、
同执行全部子任务 join/关闭、增量 CPU/内存/缓冲/临时磁盘资源归还的 owner 事实。
只能在所有 async context 成功退出后持久签发；取消、EOF 截断、关闭失败或任何
owner 不可确认均保持 unknown。controller 只能证明其管理的实例事实，不能替代
未纳入管理的处理子任务。实例 baseline 保留，不能把 execution resources_closed
解释为 instance stopped。

最小权限要求：部署资格 owner、B 处理/外发事实 producer、A 结算 consumer 分离；
运行角色无 DDL/DELETE，A 无写处理/stop/gate事实权，普通业务请求无填写处理证据权。
若 B 普通 writer 与可信 producer 共用可伪造记录的身份，缺少约束/证明链，A 不消费
正向证据。本次新增 view 权限只属于 A 的否定检查，不解决 producer 的授权链。

回滚先关闭新资格/准入，再栅栏所有可能旁路的新旧外发者；既有门、原命令、取消和
不可变证据保留。新执行不得交给无门旧 writer，旧 unknown 不补建 open。恢复采用
原身份查询或现行可信实例停止路径；不为回滚释放预算。

## P2：固定对象与未知提交的选定恢复规则

不自动重新 PUT 或重新跑 VAD。B 在外部写入前，持久保存原 submit 的 put_intent、
完整固定对象身份/预期 canonical 字节摘要/长度、处理关闭回执及其 digest。
intent 不产生终态。成功取得非空不可变 storage version 后按**该版本** GET 校验字节，
持久记录 verified receipt；seal 在短 DB 事务内消费它，事务中不调用对象服务。

| 故障窗口 | 恢复决定 |
| --- | --- |
| PUT 前 intent 未提交/提交未知 | 只查原 intent；无确认不写对象、不重建执行 |
| PUT 已提交回执丢失，无可信固定 version | 保留 pending/unknown 与预算；不能 GET latest 或凭 key/digest 猜原版本 |
| 固定 version 已获知但未记录就崩溃 | 原身份协调；没有可追溯 version 回执仍 unknown，禁止自动覆盖 |
| verified receipt 与 processing_closed 已持久，seal 前崩溃 | 查询原门/取消；按固定 version 重新验证后可恢复同一 seal，无重新处理/外发 |
| seal 已提交回执丢失 | 读取原不可变 seal；返回同一证据 ID、时间、版本、摘要和对象引用 |
| 对象被覆盖/原版本删除/摘要长度不符 | 禁止发布；不以 latest 替代，不改原事实为另一个结果 |
| A 释放/发布事务回执未知 | 分别查原 lease/发布身份；不重写对象，不覆盖已成功状态为失败 |

未绑定可信固定 version 的孤儿保持私有隔离，进入已有获准生命周期处理；本任务
没有删除它们的授权。将来允许查询 provider 的原写入身份恢复版本，必须另外固定
provider 保证与实测；仅有对象自填 metadata 不足以恢复原处理/停止事实。

发布仅消费验证后的固定版本、原请求摘要及合法终态；输出空文本仍须完整私有制品。
当前 main 不能从任意 staging/空字符串直接完成公开 Job。DEL-356 的 pin 前置候选也
没有提供生产 planner 资格、完整原子准入或公开结果发布接线。

## 最短交接与可证伪验收

下一接收人为唯一 SM+PO；由其将以下固定输入交 B（DEL-357），再串行交 A 实装新契约。
无需重新确认 no_speech 产品语义。当前缺的是实现事实与运行资格。

| Owner/交付 | 可证伪检查 | 当前状态 |
| --- | --- | --- |
| B：受管传输和旧凭据/入口栅栏清单、固定 epoch 证明来源 | 旧 writer/直接客户端真实请求必须被拒；迟到协程在 seal 后实际网络计数为零 | 缺；本次未改变运行态 |
| A/B：取消权威事务及全入口桥接 | 两连接观测 cancel/permit/seal 两种顺序；receive 提交后暂停 claim，seal 必须拒绝 | 仅上述设计；无新 gate/cancel 实现 |
| B：唯一受限处理 producer、资源 owner/权限与完整回执 | 截断、VAD异常、关闭失败、活动子任务、错 producer/generation/fencing 逐项不能生成有效封闭事实 | 缺；构造字典不能代验 |
| B：put intent/固定 version 回执和恢复查询 | 真 MinIO PUT 后丢回执、seal 前崩溃、版本删除/错摘要/长度、重启不重 PUT | 缺；MinIO 未验证 |
| A：本次既有失败分支的历史否定检查 | 每种接受/派发/staging/样本/interval 历史及锁后变化均拒绝；账目与身份不变 | 已实施；真实 PG 和原始日志见 DEL-361 交接 |
| A：将来 release_zero/Job发布事务 | 有资格的可信事实才按原 lease 结算一次，baseline 保留；实际 commit 后丢回复原身份恢复 | 正向路径未实施/未启用；本次仅验证既有失败路径的事务性质 |

本次回归不是 candidate-1 gate 互斥、可信停止、真实引擎、网络故障、双库、MinIO 或
物理拓扑验收。ADR 0007 旧原型退出条件仍未满足，本次不移除历史门禁。
非作者 Sol 须审固定最终 patch/manifest 和原始日志，重点反证最小权限、锁后读取、
默认拒绝、旧消费者回滚及全局锁序；作者自查和 QA 不代签。
