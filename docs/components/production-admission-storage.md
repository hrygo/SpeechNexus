# 完整准入的私有命令存储

属于 [A01 #20](https://github.com/hrygo/SpeechNexus/issues/20) 与
[A03 #22](https://github.com/hrygo/SpeechNexus/issues/22)。包内增量迁移
`0007_admission_commands` 增加两张 A-owned 表，不改 0001–0006，
不写入预算、租约、Job 或 outbox，也不启动执行。
该存储单元只交付基础；后续[独立完整准入事务](production-admission.md)已消费这些表。
命令解析、实际调度消费者、完整业务配额、计量结算和真实资格继续单独验收。

## 两份原请求与两种摘要

| 表 | 身份 | 保存内容 |
| --- | --- | --- |
| `admission_operations` | `operation_id`，跨 workspace/attempt 唯一 | workspace、私有业务正文/摘要、首个与当前 attempt、协调记录时间、version、正文删除 tombstone |
| `admission_requests` | `(operation_id, attempt_id)` | 原 `admit_recipe` 完整请求正文/独立摘要及记录时间，引用原 operation 和已存在的 attempt |

完整准入的业务正文由受信业务用例完成源契约校验、默认值解析和不可变资源版本固定后提供。
同步语音与同步转写已有各自的准入适配器，具体装配和限制分别见
[同步语音](synchronous-speech.md)与[同步转写](../operations/synchronous-transcription.md)；
这些接入不代表公开异步 Job 的生产 planner 已完成。
公开 Job 的权威 intent 存在 `speech_job_intents`。`asr`/`tts` 创建在同一事务内由
`admit_in_transaction` 独占写入 `admission_operations.request_document` 的完整业务命令，
公开意图继续由新表承载；两列摘要因此不同：`speech_jobs.request_digest` 与
`speech_job_intents.request_digest` 是公开规范意图摘要，`admission_operations.request_digest`
是私有执行命令摘要。`voice_design`/`voice_clone`/`voice_preview`/`voice_validation`
尚无执行配方，仍按旧路径写入非派发的公开意图副本。voice 行占用了
`admission_operations.operation_id = <job_id>` 锚点，但没有 attempt、lease、配额投影或
`coordination_jobs` 预留；A3 不得再对同一个 operation 调用 `admit_in_transaction`，
否则会触发 `reconciliation_required` 或 `idempotency_conflict`。voice 执行配方落地时应
新建 operation 身份或先把旧锚点迁移为正式预留，不得复用该行。
准入信封的摘要包含本次 authority、实例版本、阶段分配和期限，不能替代业务摘要。
准入信封的正文序列化与摘要计算在创建事务内完成，按 RFC 8785 的规范字节计算；
迁移不校验 JSON、摘要或命令是否完整，完整性由公开 Job 创建适配器在提交前验证。

创建顺序固定为：锁 submission key → 按 key 重放/冲突 → 既有 binding/草稿/校验锁 →
同一连接读取 quota、pool、host、instance、runtime plan、voice、pronunciation、asset →
`admit_in_transaction` 写 operation/attempt/request/lease 与预算投影及
`coordination_jobs`/`coordination_outbox` → 写 `speech_jobs`、事件、submission、
`speech_job_intents` → 读回 Job 后提交。任一步失败整事务回滚，不存在只写预算或
只写公开 Job 的中间态。
不得把摘要、当前预设或后来变化的默认值当作原命令。

`admission_requests` 保存的仅是冻结协调请求，含标识、版本、分配和期限，不能塞入
业务文本、引擎命令或任意扩展字段。原 `admit_recipe` 的闭合 schema、
`AdmissionSnapshot` 及七个 `query_outcome` 目标不变，没有新增公开正文入口或
Operation/receipt 投影。私有正文不得出现在日志、公开 DTO 或异常中。
表字段存在不证明原请求已经由实际业务入口生成、持久化或恢复。

## 公开 Job intent 的独立存储

迁移 `0046_public_job_intents` 增加 `speech_job_intents`：`job_id` 外键引用
`speech_jobs`（`ON DELETE RESTRICT`），`request_digest`、`request_document` 与
`recorded_at_us` 分别受摘要格式、正文长度（2–262144 字节）和时间范围 CHECK 约束。

- 创建在同一事务写 `speech_jobs`、`speech_job_intents` 与 `admission_operations`；
  新表插入不使用 `ON CONFLICT DO NOTHING`，冲突即回滚整个创建。
- 读取只查 `speech_job_intents`；无行返回 `None`（不可重放），有行但摘要与
  `speech_jobs.request_digest` 不一致时视为存储不可用；两种情况都不以
  `admission_operations` 副本回退或重建。
- 派发器的摘要取 `COALESCE(admission_operations.request_digest, speech_jobs.request_digest)`；
  当前两列摘要相等，因此不改变行为。A2 把 legacy 列改为完整业务命令及其摘要后，
  守卫使用命令摘要，公开 intent 摘要仍由 `speech_jobs` 与新表保存。
- writer 角色对新表只有 `SELECT`、`INSERT`；启动校验反向断言该表没有 `UPDATE`/`DELETE`。
- 回填只接收 `admission_operations.request_digest = speech_jobs.request_digest` 的历史行，
  摘要不符或正文已清理（NULL）的行跳过并输出 NOTICE，不伪造正文。
- downgrade 仅当每一条新表行都有摘要与正文都一致的 legacy 副本时删除该表；否则拒绝，
  避免删掉唯一的公开 intent。

## 同一事务中的锁与身份

完整准入须先按现有顺序锁完全部 pool、quota、host、runtime head、generation 和
相关 runtime intent，再锁或创建 `admission_operations`，然后写 attempt、
`admission_requests`、全部 leases、Job/outbox。同步 TTS 另有前置 binding/声音锁及
settlement 写入，见[完整准入事务](production-admission.md)。新增位置见
[协调锁序](../architecture/coordination-transactions.md#4-锁序隔离与数据库适配)。
不能先单独提交 operation、再逐 stage 预留，也不能在持有 operation 锁后反向取得父锁。
唯一键插入和外键同样可能等待，最后一次等待后仍须重取数据库时间并复核期限、
owner 与资格窗口；插入完成不是提交成功的证据。

operation 的全局主键可作为不同 pool、workspace 或新 attempt 竞争时的共同权威。
受控准入用例须在该锁下核对原 workspace、业务摘要、原命令与当前 attempt，
未解决或未释放的执行不得换 attempt 绕过。`current_attempt_id` 和 version 为以后
安全转换保留；表本身没有实现重试许可，不将唯一键冲突直接解释成安全重放。
新 attempt 必须先证明原执行结果允许、停止/释放证据及业务语义均满足；否则协调原身份。

operation 不向 quota 或 attempt 建反向外键，允许按上述顺序先创建权威再写 attempt，
避免环状插入依赖。请求记录同时引用 operation 与复合 attempt 主键；两者的 workspace、
摘要等跨行规则由完整准入事务核对，数据库不独立保护；当前恢复只校验首个和当前 attempt
标识格式，不验证 `current_attempt_id` 等于被查询的 attempt，不能将其当作已验证的指针一致性。
运行角色没有 workspace、原摘要、首 attempt、创建时间的 UPDATE 权限。
准入请求记录只允许 A SELECT/INSERT，不允许 UPDATE/DELETE。

## 正文清理与历史

业务正文非空且没有删除字段，或正文为 NULL 且删除时间、删除任务和证据摘要全部存在；
CHECK 拒绝半个 tombstone、空删除任务、早于记录的删除时间和超出安全整数的时间。
原业务摘要、身份、首 attempt、记录时间和准入元数据不随正文清理删除。
准入请求不含业务正文，保留原协调元数据用于查询和恢复。

只有 A 获正文/tombstone 列 UPDATE。这个存储能力不是删除授权：A07 的预检、
范围确认、持久删除账本、执行停止/释放核验和防恢复复活仍须由受控用例完成，
当前没有公开或内部删除端口。表内 CHECK 不比较旧行与新行，不能代替这些规则。
不能通过 SQL 权限或 tombstone 形状声称已经实现安全内容删除。
资产默认七天保留规则不自动扩展到私有命令，不在此引入命令保留默认值。

## 升级、授权与验收

以独立 owner 凭据运行既有 `speechnexus-job-migrate --schema speech` 到 head，
再审阅并安装生产 `coordination_runtime_grants` 的输出。
新建两表保持为空；历史 attempt、Job、submit、正文和 ACL 不回写、不伪造正文。
旧记录缺少原命令时，未来读取/派发必须进入协调，不能按新默认值恢复。
迁移不扩大现有角色权限；`0046_public_job_intents` 只向 Job writer 增加新表的
`SELECT`/`INSERT`，不授予 `UPDATE`/`DELETE`。破坏性 downgrade 拒绝执行。

B 对两表没有 SELECT、INSERT、UPDATE 或锁 version 的授权，不需要获得业务正文。
C 继续通过公开协议消费，不共写 speech 表。运行角色不能 DDL、删除协调历史或
调整控制面预算；部署还需检查继承和全局授权，不能只审阅生成的 GRANT。
根目录草案 DDL/授权脚本保持原保护范围，产品只使用包内迁移与生产授权函数。

`test_admission_command_storage.py` 定义 PG/InnoDB 检查，覆盖原正文与独立摘要、长正文不截断、
外键与时间/tombstone 约束、非 owner 权限、跨 workspace/attempt 的真实唯一键等待、
持锁版本竞争及私有记录与 Job/outbox 同事务回滚。
PG 另验证新迁移中途失败全回滚、重复升级和历史/授权保留。
测试定义不证明当前环境已通过；合成 SQL 存储竞争不等于完整准入业务裁决或提交未知恢复已经接入。

按仓库规则运行完整 `make coordination-test` 与 `make integration-test`，
两个隔离数据库 DSN 缺一不可；`integration-test` 还要求隔离 MinIO endpoint 和测试密钥，
具体配置见[集成测试说明](../../tests/integration/README.md)。InnoDB 只是相同约束与锁语义的对照，
不形成产品 MySQL 支持承诺。ADR 0007 的遗留原型退出条件仍未满足，保留旧必跑门禁。
