# 完整配方准入的独立事务

属于 [A01 #20](https://github.com/hrygo/SpeechNexus/issues/20)、
[A03 #22](https://github.com/hrygo/SpeechNexus/issues/22)，消费
[`0007` 私有命令存储](production-admission-storage.md)。
`admit_recipe` / `query_outcome(attempt)` 提供完整阶段预留和原身份恢复。
**业务命令解析、批准策略/真实资格 provider、调度循环、公开 Job/Operation/receipt、
完整业务配额与结算尚未装配。** 没有新增公开路由、执行进程或默认批准 provider。
合成观察和构造 plan/readiness 值不证明真实引擎、最大窗口预热、内容网络或多主机资格。

## 输入与信任边界

协调源仍为 `internal/coordination-transactions.schema.json` 1.0.0。
Python 的 `validate_coordination` 与 TypeScript coordination bundle runtime parser
覆盖全部18端口和 `TransactionResult`；字段、类型和默认值沿用同一源和生成器。
`AdmissionTransactionBoundary` 验证闭合请求，组合根另提供两个服务器上下文：

- `OriginalBusinessCommand`：业务用例先校验源契约、解析有效参数并固定资源版本，
  再交 `business_command_from_value(..., max_command_bytes=...)` 做 RFC8785 规范化与摘要。
  此函数不执行业务校验、不填默认值或解析模型/预设。文本不做 Unicode normalization；
  非有限数、不可精确表达的整数、不成对代理字符及超出显式上限的正文拒绝。
- `ApprovedAdmissionRecipe`：受信 provider 给出完整阶段集合、最小槽位、冻结
  `ApprovedRuntimePlan`、准入有效期与明确的 quota 维度。不能从客户端预算、ready
  字符串或临时默认值产生批准。缺实际 provider 时不能开放业务入口。

上下文不是新增 JSON 字段，客户端追加业务正文/批准标志会被原请求 schema 拒绝。
原业务正文和入站准入信封在事务前规范化；恢复只核验有界协调元数据，事务内不做
业务默认值解析、长编码、HTTP、对象存储、推理或容器操作。

| 记录 | `request_digest` 的含义 |
| --- | --- |
| `admission_operations`、`coordination_jobs` | 原完整有效业务命令摘要 |
| `admission_requests`、`admission_attempts` | 完整admit信封摘要，含身份、authority、分配、版本、期限及初始事件 |

JSON 对象键序不改变规范摘要；数组顺序、租约、期限或版本改变属于另一份请求。
不能用信封摘要替换业务摘要，也不能从摘要或当前可变预设重建命令。
正文不进入对象 repr、协调结果或错误信息。

## 一次事务中的预留

先读取 generation 的不可变父键提示并核对分配，再按
[全局锁序](coordination-transactions.md#4-锁序隔离与数据库适配)取得全部 pool、quota、
host、head、generation、相关runtime intent及operation。同表键排序，不逐stage锁完整链；
锁后重核对父键、当前generation和全部版本。operation主键跨workspace/pool/host排斥身份。

核对 active owner/token/期限、active pool/host、policy/quota版本、完整阶段集合及
gateway/binding/manifest/topology。runtime须是当前且没有stop intent的ready/steady
generation，具有0006保存的完整原prepare、成功观察及私有readiness证据。
证据身份、fencing、资格摘要和有效期必须匹配；手工ready字符串或旧缺失记录不能放行。

纯 domain 一次计算全部预留：

- 每stage使用冻结generation的保守per-slot向量乘槽位。同generation多stage分别累加，
  不重复收取runtime peak/steady基线。
- 六维满足 `held + quarantined + new <= allocatable`；unified和离散内存分开，
  CPU decode、PCM buffer、scratch不省略。跨pool竞争同一父host。
- 全部held/quarantine槽位占总上限；非realtime不借用realtime floor，已经由realtime
  占用的保留槽位不再次扣除。乘法、累计和版本先在Python有界检查。
- 并发attempt quota一次增加1，与stage数无关；隔离继续占限额。

同事务创建operation原命令、attempt、原准入请求、全部leases、六维/槽位/quota投影
与初始Job/outbox。Job使用同cursor的`create_in_transaction`，不调用另一个提交端口。
预先独立提交的coordination Job或可见的不完整operation不自动补成准入；历史adoption、
业务Operation/inbox与公平队列仍待受控用例接入。失败全部回滚，不逐stage补偿。

operation/lease/Job/outbox唯一键与FK均可能等待。最后一次可能等待的写入之后，重新取
数据库时间，复核请求、owner、批准窗口和readiness有效期，再允许提交。
取消、断线或期限到达不释放已提交的执行预算。

## 配额范围显式

当前DDL只具体化并发attempt quota；批准计划必须明确声明
`quota_dimensions=frozenset({"concurrent_attempts"})` 才可使用当前独立端口。
若策略要求输入字节、音频时长、生成样本等配额，而相应reservation ledger尚不能参加
同一事务，本端口拒绝准入。不能省略所需维度或把未知额度当0。
本单元不宣称完成A03全部业务配额、输入去重与结算，也不另写B的PCM区间账本。

## 重放、未知与释放

同operation/attempt、业务摘要及完整信封精确匹配时，只读返回原lease与预留，不因
owner、当前策略或runtime改变而重新分配。物理向量保留，快照version反映当前attempt
版本；隔离/释放后查询不能推导出仍可执行。原请求、阶段集合、lease或Job/outbox缺失/
不一致时进入协调，不修补数据。私有正文tombstone不恢复内容、不授权派发。

异命令或异分配/期限/版本冲突。已存在operation的新attempt当前保持关闭，即使原lease
释放也不自动获得许可；以后须由受控业务用例证明尚未终态的Operation可安全重试，
原执行已结束且原意图一致。公开Operation终态不得翻转。未知只能查原身份，不能换attempt。

PG adapter使用现有有界SpeechPool及逐语句剩余超时。只有确认回滚的死锁/序列化中止
可在归还连接后有限重试；唯一键冲突只读恢复。提交确认阶段的连接、取消或其他不确定
错误返回 `unknown/query_original`，不作普通重试。查询明确READ ONLY；原attempt不存在
仍为unknown，不能许可新执行。

后续由[隔离/释放事务](production-leases.md)消费B的持久事实：unknown保持总占用，
确切停止/释放或可信从未外发事实后才退当前lease增量，最后required stage才退attempt
quota。runtime基线另走停止流程。A07预检、正文删除许可和恢复账本仍待接入；资产默认
七天保留不扩展为命令保留默认值。

## 组装与验证

组合根使用 `AdmissionService(PostgresAdmissionTransactions(pool, max_command_bytes=limit))`
及 `AdmissionTransactionBoundary(..., decode_request=request_from_wire)`；上限显式配置。
迁移仍到0007并安装已有生产最小角色。本单元不改历史迁移、不扩权；B不读私有原正文，
C继续通过公开业务协议消费。

`test_production_admission.py`验证PG/InnoDB真实父锁/operation/唯一键等待、共享末预算、
完整配方回滚、两架构六维、最后等待后的四类有效期、原身份查询及native runtime→admit→
B提交/观察/unknown/stop→A隔离/last-stage释放。PG另验证实际提交后丢确认，含取消错误。
批准/readiness/停止证据为合成事实，不是引擎或网络资格；单元故障游标只验证驱动分界。
统一26个请求正例、24个负例和22个结果由Python/TypeScript同版parser消费，不改写输入。

运行完整coordination/integration、Python、契约/漂移/兼容和Web门禁后交付。
ADR0007退出条件尚未满足，保留旧必跑门禁；完整消费者与真实资格继续验收。
