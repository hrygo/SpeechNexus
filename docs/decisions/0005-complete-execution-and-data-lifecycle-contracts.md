---
title: 完整执行命令、公开投影与数据生命周期契约
status: accepted
date: 2026-10-23
---

# ADR 0005：完整执行与删除契约

整体架构审查确认内部命令、同步恢复、receipt、数据删除、容量与证据保护存在缺口。
当前只有运营接口和 Web 外壳，没有已发布语音业务运行态。本轮按用户授权修复
架构、契约、工具、测试和协调 DDL 草案；另仅补齐现有运营 DTO 的声明约束。

决定如下：

- Submit 显式包含严格、按任务/阶段判别的完整有效 command；SHA-256 摘要基于
  RFC 8785 canonical JSON。移除孤立 input_asset_ref，用受控、带长度/摘要的
  已解析资源引用替代。内部 control 0.2.0.dev0 和 stream 1.1.0 的生产者/消费者
  必须同步升级。文本流冻结 append/commit/finish/ack、字符额度及精确重放。
- Receipt 的 requested/effective/observed 使用严格公开字段投影，禁止递归任意
  JSON。schema 修订标记为 2.0.0；这是 Nexus receipt 的有意收紧，不允许旧生产者
  透传未定义字段。没有线上历史 receipt 迁移；未来若接入既有私有部署，应先离线
  投影并验证，不能回退到任意 JSON。公开 OpenAI 标准字段不改变意义。
- 同步 409 错误使用 error.nexus.operation_id 恢复原 Operation；
  response_not_replayable 还必须带原成功 receipt。扩展不覆盖标准 error 字段，
  不因内容不可重放再创建执行。Python 和 JavaScript SDK 都验证该投影。
- 删除采用每模块自有的预检 plan、版本/摘要 pin、完整范围确认、异步 Deletion
  及独立账本。跨模块独立保存副本不会被源资源删除追赶。停止未确认保留预算；
  内容删除不删除执行/fencing/计量证据。恢复先重放删除账本再开放内容。
- 协调历史及水位作用域禁止删除、截断与改名，runtime intent 全部请求字段冻结。
  每维 lease 预留至少等于 slots × 固定 per-slot 预算；多 stage 共享实例采用
  经核验的逐维保守上界，分别累计。运行角色分离、非 owner，无 DDL/历史删除权。
- 所有事务模板及隐式触发器遵循一致锁序；容量验证只扫描活跃集合，通过私有
  事务队列去重，保留审计历史。接口与基础设施是独立同层，两个业务上下文及
  backend/gateway 互不导入。公开客户端仍可以从基础设施边界调用公开协议。
- 契约门禁按请求可接受域与响应保证区分方向，解析引用及组件；复杂组合约束
  无法静态证明等价时保守报错。禁止通过删减测试或放宽 schema 掩盖漂移。

详细语义分别维护在[执行命令](../architecture/execution-command.md)、
[数据删除](../architecture/data-deletion.md)、
[协调事务](../architecture/distributed-coordination-schema.md)及机器契约中。

契约、fake 序列和 DDL 实验不证明真实业务服务、模型质量、吞吐、停止能力或部署
网络隔离已经实现。后续实现必须复用相同正负/故障序列，提交真实 adapter 和部署资格。

## 后续决定

[ADR 0006](0006-cluster-coordination-and-storage-portability.md)替代本决定中强制由
数据库独立执行跨表规则、以延迟验证队列完成容量校验的实现要求。
历史保护、预算、锁序、执行/停止证据及全部协议语义继续有效。
现有 DDL 与测试保留其原型验证事实，替代路径须先通过新的受控事务行为验收。

## 2026-10-23 实施补充：内部控制错误恢复

内部 execution-control 升级至 `0.3.0.dev0`，为真实 HTTP 屏障补齐控制存储不可用、
提交结果未知、状态待协调和证明冲突。新增控制码不进入既有持久执行错误枚举；
schema 固定 `retryable` 与恢复提示的组合，提交未知不能伪装可直接重试。
屏障恢复沿用原 gateway/pool/owner/token，可重新确认原屏障，不重新外发引擎请求。
新增字段和控制码要求旧严格错误消费者同步升级生成绑定；不假定旧校验器能接受。
公开语音协议与既有执行/停止事实保持原义。配置、故障矩阵与已验证边界见
[执行网关](../architecture/execution-gateway.md#7-认证-http-控制与运行装配)。

## 2026-10-23 实施补充：未知外发的恢复

内部 execution-control 升级至 `0.4.0.dev0`。崩溃后的 intent 可能没有外发时间或
engine request ID，但整实例终止仍可提供可信停止与资源释放事实。为避免永远无法关闭，
仅允许 unknown → aborted 凭 controller/operator 整实例终止双证据闭合，并保留空外发事实。
engine ack、executing、succeeded 和有清理的 failed 仍要求真实外发时间；
证据须可信覆盖原 generation，且不能代替入口屏障或代际凭据保证。

Submit 重放在此分支必须携带 `stop_evidence_kind`；只读查询返回原快照，
新增 `control_execution_not_found` 固定为 404、retryable=false、recovery=none。
404 不能证明引擎未执行。旧严格消费者会拒绝新状态组合或新错误码，须同步升级绑定与样例。
不改变公开语音协议，不回填历史外发时间；B 仍不归还 A 的预算。细节见
[执行网关恢复](../architecture/execution-gateway.md#8-未知执行与只读恢复)。
