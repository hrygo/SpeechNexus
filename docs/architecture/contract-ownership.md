# 契约与协调事务交接责任

本文件配合 [A00 #19](https://github.com/hrygo/SpeechNexus/issues/19)。机器矩阵为
[`coordination-ownership.json`](../../contracts/coordination-ownership.json)，
测试核对 18 端口、18 张生产协调表和 `query_outcome` 快照映射；根目录草案仍为原15表。
矩阵描述目标归属，不证明端口已实现。生产事务进度继续在 A01/B00–B06 跟踪。

冻结 main `7dc276a` 的实际端口、7 类查询、正式消费者及双库证据见
[2026-10-25 取证快照](../audits/2026-10-25-coordination-port-evidence.md)。
该报告分别记录 adapter/统一结果/业务装配与剩余验收，不改变此处的机器所有权。

## 契约生产者与消费者

| 契约                                 | 实现/事实所有者             | 消费者                            | 交接状态                                          |
| ------------------------------------ | --------------------------- | --------------------------------- | ------------------------------------------------- |
| speech OpenAPI                       | A                           | C 工作台后端/Web、MCP；A 公共接口 | 首批文件闭环类型/校验见独立契约包；实际路由待实施 |
| admin OpenAPI                        | A；B 提供实例/资格事实      | C 管理界面                        | 源契约和变更清单；生成/消费联调待补               |
| workbench OpenAPI                    | C                           | C Web/MCP                         | JSON/HTTP入口与样例已交付（#73/#75/#77）；真实消费者待验收 |
| realtime events                      | A；B 提供执行流事实         | C 浏览器/A 实时入口               | 公开session/事件parser与时序输入已交付（#79）；真实时序/WS待验收 |
| execution control / stream / command | B；A 编排调用方             | A Scheduler/Worker/实时服务       | 源契约；生产联调待补                              |
| runtime control                      | B 控制器；A 生命周期编排    | A/B                               | 源契约；生产联调待补                              |
| coordination transactions            | A speech/runtime；B gateway | 各自 application 与 adapter       | 下表冻结归属；生产事务分别实施                    |

公共契约、生成器、锁文件和 CI 由 A 串行集成。C 不导入 speech 内部模块，也不共写
speech 表；B 不接管 A 的预算释放裁决。首批独立包的安装、类型、运行时校验和样例见
[`packages/contracts`](../../packages/contracts/README.md)。Python 使用生成 TypedDict
加权威 JSON Schema 校验；类型本身不承诺状态或跨字段约束。

## 18 个端口

A02 的 `0009_profile_drafts` 另加三个 speech 配置私有表：
`speech_profile_families`、`speech_profile_revisions`、`speech_profile_commands`。
它们只支持不可变草稿分配/原键恢复，不扩共享协调端口，不授权 B/C 写入；
见[草稿端口与迁移边界](profile-drafts.md)。

| 端口                                                  | 主责           | 权威写入范围                                                             |
| ----------------------------------------------------- | -------------- | ------------------------------------------------------------------------ |
| acquire_owner / renew_owner / activate_owner          | A              | resource_pools                                                           |
| admit_recipe                                          | A              | operation/私有原请求、quota、host/instance 投影、attempt、完整阶段 leases、Job/outbox 同一事务 |
| create_job                                            | A              | coordination_jobs、coordination_outbox 同一事务                          |
| claim_job                                             | A              | coordination_jobs                                                        |
| quarantine_lease / release_lease                      | A              | lease、attempt、quota、host/instance 投影                                |
| create_runtime_intent / observe_runtime               | A              | runtime head/intent、instance、host；创建 intent 同事务 outbox/私有投递账本           |
| seal_barrier                                          | B              | gateway_fencing_barriers                                                 |
| create_submit                                         | B              | submit_intents、output_stream_heads                                      |
| mark_execution_unknown / record_execution_observation | B              | submit_intents                                                           |
| record_stop                                           | B              | execution_stop_evidence、submit_intents                                  |
| redact_execution_command                              | B              | submit_intents 正文清除与删除证据；不删最小执行事实                      |
| append_interval                                       | B              | output_stream_heads、output_sample_intervals、submit 计量投影            |
| query_outcome                                         | 对应事实 owner | 只读原身份，不发起执行                                                   |

机器矩阵逐个列出 producer/consumer 和表。表名不授予任意列修改权；实际最低权限以
[`coordination-grants.py`](../../scripts/dev-env/coordination-grants.py) 和版本化迁移为准。
配置列由控制面维护，运行角色不能提高配额或预算；跨 owner 的锁定版本列不代表
业务所有权转移。该表是协调表范围，不是全产品表清单。
`0007_admission_commands` 新增的 operation/准入请求表仅 A 可读写，B 不获跨 owner 锁授权；
详见[准入命令存储](production-admission-storage.md)，独立admit_recipe事务见[准入实现](production-admission.md)，完整业务配额/消费者与真实资格仍待。

`0008_runtime_delivery` 新增 A 私有投递账本，创建 runtime intent 时同事务插入；
专用投递角色只能认领、标记未知和确认传输，不能改写原请求、物理状态或预算。
B 无该表权限，私有投递端口不扩充共享18端口；见[持久投递](production-runtime-delivery.md)。

## 未知结果恢复映射

| 查询目标       | 快照              | owner |
| -------------- | ----------------- | ----- |
| pool           | OwnerSnapshot     | A     |
| attempt        | AdmissionSnapshot | A     |
| job            | ClaimSnapshot     | A     |
| submit         | ExecutionSnapshot | B     |
| lease          | LeaseSnapshot     | A     |
| interval       | FactSnapshot      | B     |
| runtime_intent | RuntimeSnapshot   | A     |

unknown 只能查询原身份或协调，不能新建执行普通重试。B 的区间账本由 A 消费用于
配额与结算。取消请求、TTL 和断线不是停止/释放证据。完整锁序、跨表裁决及最低
证据要求仍以[协调事务设计](coordination-transactions.md)为准。

[租约隔离与释放事务](production-leases.md)提供 A 的 quarantine_lease、release_lease 与只读 LeaseSnapshot
入口，按权威事实保留/结算六维增量、槽位和最后 stage 的 attempt 配额。独立admit_recipe已接入同事务预留；
parser、数据库接线和真实业务/引擎资格分别验收。
