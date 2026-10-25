# C06 持久纪要任务与最终发布事务实施计划

> 沿用已授权 native inline / TDD；一次独立只读终审。基线 `a98b2bb`，保留183文件包 `9dcadc72`。不提交、推送、启用模型或修改共享迁移入口。

**Goal:** 将前一单元的固定源 LLM 能力接入真实工作台持久任务，保存原意图/attempt/用量，支持显式原意图重试、业务取消及最终源版本/删除屏障；实现自己的 owner migration fragment 和真实 PG 验证。依据 [ADR 0005](../decisions/0005-complete-execution-and-data-lifecycle-contracts.md) 和[删除规范](../architecture/data-deletion.md#执行证据和查询)保留无正文执行/计量事实。完整 C06 的 Meeting 映射、公开生成 DTO/路由/页面、独立保存制品、助手和实际部署资格继续推进。

**Spec:** [ADR 0003](../decisions/0003-workbench-llm-network-boundary.md)、[技术方案 §9](../architecture/technical-blueprint.md#9-工作台与内网-llm)、[C06](2026-10-23-three-team-delivery-plan.md#43-c-团队应用与体验)、现行 workbench MinutesJob；本单元内部类型不冒充公开 DTO。

## 设计与约束

- 复用既有短事务 UOW 与 parent→transcript→task 锁序，任何 LLM I/O 均在连接释放后。固定 source revision ID/摘要与完整不可变 LlmProfile，另保存服务端 endpoint/资格引用/profile 的不可逆 binding 摘要（不含凭据）；任务重试不能换模型/模板/源，换配置是新意图。
- 工作台独立 task-content/immutable-intent/attempt/request-key/retry-key 表；原创建键按 workspace hash 唯一，取消/失败/提交回复丢失后按原身份查询，不自动创建替代任务。queued 才能领取，attempt token 隔离迟到结果；running 丢失进程后不能自动重投。
- 创建、领取、查询和最终发布均检查可见源与当前 head。新稿使候选 expired；删除接受立即隐藏任务与结果。发布检查与正文 UPDATE 同一事务，业务取消不会被迟到结果覆盖。
- 取消监测以短查询执行，取消本机 HTTP task 并收尾 attempt，不声称远端计算已停止或释放父预算。工作进程自身取消记录失败/中断事实，自动重试关闭，调用方显式重试同一固定意图。
- 候选是 source-owned 内部行。FK CASCADE 只在所有 attempt 已确认请求正常完成或从未领取时仅清除 task 内容/profile 投影；无正文 intent、attempt/计量/完成证据及幂等摘要不随内容级联，遵守 ADR 0005；未确认的运行/失败/取消请求由数据库 guard 阻止物理清理，等待后续具备资格的停止/对账入口，不能将 HTTP 取消当停止证据；独立无正文 request-key hash 记录保留以拒绝旧键重建。公开 Meeting/Minutes 删除资源接入前须扩展完整依赖图，当前不宣称该 wire 能力已完成。
- runtime 只能追加冻结意图/attempt/key，及更新任务状态、结果和attempt用量；不能修改源/冻结 profile、执行 DDL 或删除业务行。迁移仅 owner 显式执行，A 协调生产框架；不改旧 fragment、不运行生产升级。
- 保留上轮全仓网关时钟 fixture 阻塞，B/A 候选仍不应用权威树。C 范围与临时组合门禁分别报告。

## Task 1：状态与应用端口

新增 domain/minutes_tasks.py、application/minutes_tasks.py、tests/unit/backend/test_workbench_minutes_tasks.py。

RED→GREEN：严格状态/版本/attempt token 与时间，取消/过期单向，重试固定意图；无正文 repr；默认关闭不创建或读取任务；异常/取消不自动重投。应用协调端口与运行过程不引入 SQL、公开 DTO 或 speech 内部依赖。

Expected：针对性单元全通过，相关状态/应用分支覆盖 >90%。

## Task 2：PG 事务、发布与删除

新增 infrastructure/minutes_tasks_schema.py、infrastructure/postgres_minutes_tasks.py、tests/integration/test_workbench_minutes_tasks_postgres.py；修改既有 PG probe 加入本模块及针对性单元，不移除原测试。

RED→GREEN：owner 升级/运行角色权限、并发同键创建/异意图冲突、重复领取与旧 token 拒绝、固定 profile/原意图重试、进程重建查询、等待 LLM 不占 runtime 连接、取消与迟到结果、源变化与删除/cleanup、提交成功回复丢失后原身份恢复。记录 attempt 的业务执行与已知用量，不把未知用量填0。

Expected：socket-only 临时 PG 全通过无 skip，退出 stopped=true、独立 pid/pg_ctl 核验。Python format/lint/mypy/imports、后端/契约、28 contracts/drift、相关覆盖通过；全仓原样树与时钟候选临时组合分别验证。Web 未变则复用既有证据。

## Task 3：审查与交接

源冻结后一次 fresh sol_worker high 只读终审；Critical/Important 一轮 TDD 修复，Minor 记账。更新 handoff/goal/索引和累计 patch/manifest，验证旧文件归属、clean base apply、全字节、链接、index 不变；保持完整 goal active。

## Review Focus

1. 短事务锁序、重复执行和不确定提交不能重复模型调用或替换意图；running 不自动再领取。
2. 创建/最终发布与删除接受、源修订、取消之间没有迟到正文复活；清理不留派生正文或可重放副本。
3. append-only profile/attempt/key 的权限及约束真实生效，runtime 不 DDL；对象/模型与真实资格不越界。
4. attempt token/重试键重放隔离和已知/未知用量准确，默认关闭、错误脱敏与取消收尾可靠。
5. 公开路由、Meeting 关系、独立保存/导出、助手、实际 LLM/网络/物理预算资格仍需交付，不能把本单元视为完整 C06。

## 单元交付状态

本单元内部持久任务、一次独立终审和两项Important的同轮RED→GREEN修复已完成。最终原样unit/contract仍有独立网关时钟夹具失败，临时组合候选1526 passed；隔离PG262 passed/无skip。完整公开C06及C00–C08目标继续active。累计191文件交付和边界见[本单元交接](2026-10-24-team-c-c06-tasks-handoff.md)。
