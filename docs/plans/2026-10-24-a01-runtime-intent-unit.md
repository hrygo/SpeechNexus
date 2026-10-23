---
title: A01 runtime 意图持久化与恢复单元
status: active
date: 2026-10-24
---

# A01 runtime 意图持久化与恢复单元

属于 [A01 #20](https://github.com/hrygo/SpeechNexus/issues/20)，消费 B02 已合并的
控制器协议与进程。目标是先持久记录、预留及查询原意图，再经 outbox 派发；
不在数据库事务内调用控制器，不把合成容器证据当作 GPU 或多主机资格。
本文件记录实施路线，不证明下列端口已实现。

## 实施顺序与验收

1. **存储与快照缺口。** 保持 0001–0004 DDL 不变，以新迁移
   `0005_runtime_intents` 扩展既有 outbox：Job/runtime 判别，互斥且完整的目标外键；
   旧 Job 事件保留身份与默认语义。持久化意图创建/更新时间与原观察正文，
   历史缺少事实的意图不盲填当前时间。协调快照以 `runtime: null`、冻结 request
   和协调记录时间表达“尚未观察”，区别于真实 `absent`；不扩展控制器的观察状态枚举。
   有观察时保留实际控制器响应，包括控制器自己的创建/更新时间，不能混用两方时钟。
   未观察快照不能证明 ready/stopped 或资源释放，observe_runtime 成功必须有实际观察。
   同步契约、类型、消费者正负例及最小授权。
2. **create_runtime_intent。** 领域与应用使用窄端口；服务端提供明确的不可变模板、
   绑定、manifest、loading policy、slots 与资源向量，不从请求选择任意镜像/挂载。
   按池→host→head→instance→intent→lease/submit→outbox 顺序锁定并重核对定位信息；
   锁后取 DB 时间。prepare 同事务预留完整 peak、创建不可变 generation/head/intent
   与 outbox；stop 不释放资源，须保留 drain 前提。旧 generation 未停止并释放前，
   不能建立替代 generation。完全同原意图重放，异摘要冲突；未知只查原 node/intent。
3. **observe_runtime。** 核对完整身份、原 request digest、generation、版本、
   fencing 与观察语义；ready 后才能 peak→steady，确认停止与资源释放后才返还预算。
   迟到观察不改当前 generation；无证据/unknown 不返还预算。保存完整原观察与时间，
   终态事实不可重写。query_outcome(runtime_intent) 只读恢复完整快照。
4. **独立持久派发。** 显式 scheduler 身份、组件凭据、批准端点与有限并发；
   事务提交后外发原冻结命令，丢通知靠轮询，重启查询原 node/intent。
   不因超时创建新 generation 或重复预留。运行时不安装 DDL/授权，不持 Docker socket。
5. **交付与联合资格。** 各阶段用短 PR 排队，记录 source/head、迁移、接口、
   双库与故障证据，更新 #20/#29 与总控矩阵。受保护 CI 通过后合并。

## 证据边界

- 生产使用 PG；测试 InnoDB oracle 只转换必要的 DDL/时钟驱动差异，
  使用相同生产 SQL/领域规则。涉及本单元的 `make coordination-test` 与
  `make integration-test` 均运行，具名共享行为不以跳过代替验证。
- 覆盖父物理预算竞争、事务中途失败整体回滚、完整意图/事件身份重放与冲突、
  generation 高水位、旧实例 drain、锁后期限、提交未知、只读恢复和最小角色负例。
- 冻结迁移升级需保留旧 Job/outbox、既有授权与历史；失败升级整体回滚。
  运行角色不能修改配置/容量、私有旧请求、创建时间或删除执行历史。
- A 的受控存储/派发不证明 B 的 GPU/模型、真实停止、多主机与资源上界。
  B 提供已批准模板/模型与具名资格；C 提供真实产品消费者。缺少这些输入时，
  继续独立事务验证，并将联合资格明确保持待联调。
- ADR 0007 逐项映射不变量；未迁移规则继续有门禁，满足退出条件后清理旧机制。
