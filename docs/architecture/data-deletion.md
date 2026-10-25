---
title: 数据删除、依赖裁决与恢复屏障
status: active
version: "1.1.0"
date: 2026-10-25
---

# 数据删除与依赖裁决

本规范定义完整删除契约。Workbench 已实现项目与稿件删除路由、持久账本及启动重放，
覆盖范围和保守阻断见下文；Speech 删除路由与跨模块停止协调仍待实现。
执行网关已实现需显式可信许可的[命令正文清理事务](execution-gateway.md#10-redact_execution_command)，
未提供默认许可签发或 HTTP 清理入口，不能据此声明完整删除闭环可用。机器定义为
[deletion.schema.json](../../contracts/shared/deletion.schema.json)，入口分别是
Speech `/v1/nexus/deletion-plans`、`/v1/nexus/deletions` 和 Workbench
`/v1/workbench/deletion-plans`、`/v1/workbench/deletions`；查询使用对应的
`GET .../deletions/{deletion_id}`。两个模块只写自己的表和对象；跨模块通过公开
协议协调，不读取或删除对方业务表。接口不引入身份、私有空间或 ACL。

## 预检和确认

1. `POST deletion-plans` 携带 `Idempotency-Key`、资源 owner/kind/id/revision 和
   `mode=ordinary|cascade`。Owner 必须匹配入口，否则 `422 deletion_owner_mismatch`。
   资源不存在返回 `404 resource_not_found`。部署/工作空间仍遵循当前入口规则。
2. 所有者在短事务中读取目标版本与完整依赖快照，建立不可变 plan。依赖按
   `(owner,kind,id,revision,relation)` 排序，不得重复同一关系。
   `plan_digest` 为移除自身 `plan_digest` 字段后对完整 plan 做 RFC 8785 UTF-8
   canonical JSON 的 SHA-256；包括 id、revision、过期时间、eligible 和全部依赖。
   plan 默认有效期 300 秒，服务端返回明确 `expires_at`，客户端不能延长。
3. 普通删除的持久引用或活动执行均为 `blocks`，`eligible=false`。级联只能包含
   同一 owner 的 `cascade` 依赖；所有活动执行必须进入停止协调。不能自动删除
   其他模块资源。跨模块引用仍阻断，调用方先在对方模块独立处理，再重新预检。
4. 已保存副本为 `independent_copy / retain`，包括其独立对象、receipt 和版本。
   Speech Artifact 删除不会追删 Workbench saved asset；删除项目也不删除其曾
   引用的 Speech Artifact。项目拥有的稿件/会议/有声书可显式列入级联。
5. `POST deletions` 携带 `plan_id`、`expected_plan_revision`、`plan_digest`、
   `confirm_targets` 和 `Idempotency-Key`。确认集合必须恰好等于目标加全部
   cascade 依赖，不能确认 blockers 或独立副本。它是明确级联确认，不提供绕过
   停止证据的 force 标志。确认缺失、多余、重复或跨 owner 返回
   `422 deletion_confirmation_mismatch`。
6. 首次接受时，同事务验证 plan 未过期、目标和所有依赖版本仍一致、依赖集合未
   新增/删除且 eligible；原子写入删除意图、资源 tombstone 和 outbox，关闭新
   引用及新执行入口。写引用/提交执行也必须锁同一资源版本状态；不能先预检再无锁
   删除。过期返回 `409 deletion_plan_expired`，快照/摘要/版本变化返回
   `409 deletion_plan_changed`，阻断依赖返回 `409 deletion_dependency_conflict`。
   均不得产生部分级联。重新预检获得新 plan。
7. 幂等作用域为 `(deployment,workspace,owner,operation_family,key)`；相同意图返回
   原 plan/Deletion，key 相同而摘要不同返回 `409 idempotency_conflict`。必须先
   重放已接受意图，再检查旧 plan 的当前过期时间；重放不创建新清理或新推理。

## 执行、证据和查询

`Deletion` 是独立清理资源，不把删除伪装成推理 Job。状态为 queued → running →
succeeded，或进入 blocked/failed。blocked 记录依赖或停止未确认的稳定错误；
取得新证据后可以继续 running。failed 是清理终态，重试用新意图并保留原记录。
后台按持久 outbox 和幂等步骤推进，不在数据库事务里进行 HTTP、推理或对象删除。

活动执行先请求 abort/drain，再查询原执行获得停止和资源释放证据。未确认时进入
`blocked / quarantined`，错误为 `deletion_stop_unconfirmed`；保留原容量账目，
不得因删除命令、客户端断线、deadline 或 tombstone 而归还预算。已发布声音的
引用材料在声音版本仍可用时是持久依赖。声音版本先 tombstone、阻断新绑定并
协调活动执行，才可删除其引用材料。不可变版本身份和摘要继续保留。

清理使用已 pin 的对象摘要和对象版本；共享或仍可写的命名空间不能按前缀追删后来生成的新对象。每一步
写入持久删除账本，记录意图、资源版本、对象摘要、完成证据与时间，不保存正文、
声音特征或凭据。临时对象失败可以重试；不把部分清理声明为 succeeded。成功必须
`cleanup_state=complete`，result 包含确切 deleted 集合、保留副本和 ledger_id；
结果不得漏掉计划确认的目标或包含未确认资源。

删除只移除业务内容与允许删除的配置投影；执行/计量/租约/fencing 的不变量证据
不在级联范围。`operation_content` 删除正文/制品，不删除执行身份、状态、脱敏
receipt、幂等摘要或计量事实。保留 tombstone 和最低 fencing 水位的审计归档由
迁移角色执行；运行角色不能 DELETE/TRUNCATE 协调历史。

## 备份恢复

删除账本独立持久保存且随备份有恢复水位。恢复快照后，先重放晚于快照水位的
所有删除和 tombstone，再重新开放内容查询、引用创建与执行入口。重放同一
ledger_id 必须幂等；账本缺失、对象版本不确定或执行状态未知时保持不可见并
进入协调，不能把被删除内容恢复为 ready。恢复演练须证明源 Artifact 删除后
独立保存副本仍可用，以及已删除材料不因恢复重新暴露。

## Workbench 当前实现与恢复边界

项目预检在项目行锁内保存私有内容快照，覆盖有声书、章节、渲染单元及其生成记录、
导出和保存副本的版本与对象身份。公开 plan 仅返回依赖身份，不暴露对象地址或正文。
确认和行清理分别复核同一快照；普通删除阻断持久依赖，级联要求逐项确认。
生成历史缺少权威停止证据时保守阻断；取消或历史状态不是停止证据。

保存副本在下载前持久化完整原始意图、资源身份和写入 token；导出在 PUT 前保留
固定导出与制品身份。所有者必须以 token 条件更新进入 writing 才可 PUT。
reserved 的显式原键重放可以接替尚未 PUT 的旧所有者，旧 token 不能写入或释放新
所有者。writing 或 unknown 不按超时、取消或普通重试释放。对象 PUT 结果未知时
保留原始意图和对象摘要，项目删除维持不可接受，直到有权威完成或停止证据；当前
没有把未知 PUT 自动判成功的恢复器。数据库发布响应丢失不产生补偿删除或新导出。

导出对象使用项目私有的 `{project_id}/exports/{export_id}/{artifact_id}`；项目
接受删除后关闭新写入，且不存在未决写入时，才可清理该项目私有前缀。旧格式
`{book_id}/{export_id}/` 只按预检固定的导出身份逐个清理，不扩大到整个书籍前缀。
独立项目副本和未确认导出身份保持不变。对象 I/O 不占数据库事务。

行清理与对象清理各有持久完成标记。公开 succeeded/result 只在两者都完成时
投影，原始行账本不单独构成公开完成证据；稿件没有项目对象清理阶段时显式记录
该决定。对象配置丢失或失败不能隐藏尚欠的清理。启动先重放账本，完成后才开放内容。

这些表由 owner 角色初始化，运行角色不执行 DDL。当前 schema revision 11 是
新建隔离部署基线；已有数据库仍需 A07 的受控版本迁移，不自动升级。缺少私有内容
快照的旧项目删除计划不得直接放大清理范围，应保持阻断并核对原确认与恢复证据。
本节不代表真实引擎停止、跨节点拓扑或完整阶段 AC 已验收。
