---
title: 零外发身份、取消与只读恢复契约核心候选
status: proposed
version: "1.0.0"
date: 2026-10-31
---

# 零外发契约核心候选（DEL-417）

本候选交付 [canonical](../../contracts/internal/zero-dispatch.schema.json)、
`zero_dispatch` 生成类型/严格校验器及[合成样例](../../tests/fixtures/contracts/zero-dispatch.json)。
它没有 HTTP 路由、生产事务适配器、DDL 或运行配置；默认不可启用。
生成包只校验形状，不证明可信资格、跨行身份相等、摘要正确、持久重放或数据库只读性。
现有成功、停止、清理、释放契约与执行实现不变；不得用此候选结算资源或通过产品 Gate。

固定 base 为 `a1ad444db343d76699e83b7f93d8a93355db7b3c`。
正式接收 DEL-416 `handoff` revision `5c9edb4c-f856-46d6-be47-0c48cbc8687c`、
`independent-review` revision `48c6efb5-48a2-4c85-ba66-658a236995d5`，
及 `candidate-patch` revision `32eb45f4-70fe-47cb-9258-3f2276599b56`。
原 patch 为 27076 bytes，SHA-256
`19fda367ab2d1f1d1bb56e58d1cee4a36720427ce8de74e8e33dbf860dd183ce`。
正式 handoff 正文比 patch 后像少一个末尾 LF，除此之外逐字节相同；原文和 patch 分别保留。
未叠加 DEL-415 实现或 DEL-386 实验，也不把它们的历史通过数计为本轮验证。

## 生产者、消费者与固定接口

A 是 canonical、生成链与本契约测试的唯一写者。B 接续实现 receive/query 的窄应用端口、
边界转换、数据库事务和最低权限；A 不写 execution-gateway 实现。
`contracts/bindings.json` 显式选择以下六个入口，生成 Python `speechnexus_contracts.zero_dispatch.parse`
及 TypeScript `@speechnexus/contracts/zero-dispatch` 的 `parse`，不生成客户端。

| 入口 | 数据与含义 |
| --- | --- |
| `OriginalExecutionKey` | 原完整 19 字段身份、原 `request_digest`、原 `output_stream_id`；只用于查找/核对，不授予写权限 |
| `ReceiveCancellationRequest` | `schema_version="1"`, `operation="receive_cancel"`, original, 完整原 AbortExecutionRequest；不接受 caller 的 epoch/receipt/expected version |
| `ReceiveCancellationResult` | 原身份、`recorded/replayed`、不可变 cancellation 记录及一致的当前 gate 观察；接收完成不等于取消获胜或停止 |
| `OriginalRecoveryQuery` | `operation="query_zero_original"`，固定原身份；独立只读端口，无动作参数 |
| `OriginalRecoverySnapshot` | 原身份、found/missing/incomplete/unknown、结构化 reason/recovery、nullable gate/cancellation；found 只表示查询完整性 |
| `ZeroDispatchControlError` | 直接复用现有 ExecutionControlErrorResponse，保留 unknown/reconciliation/conflict/rollback 恢复分类 |

`SubmissionIdentity` 的每个字段 `$ref` 指向现有 SubmitExecutionRequest 对应字段，
两条音频配对约束也直接引用其 `allOf`。因此不复制域、不改变可省略/可空 voice、
非 PCM 的 null 上限/采样率、PCM 输出流的规则。生成器新增 JSON Pointer 数组下标解析，
保留这些引用；非法、负数、前导零、越界下标拒绝。

`request_digest` 沿用 native SubmitRequest：对完整有效
`{execution_request, output_stream_id}` 的 RFC 8785 UTF-8 字节作 SHA-256；
不是仅对 command。取消摘要对完整 AbortExecutionRequest 计算，包含 reason 和 wait_timeout_ms。
时间先沿 native serializer 规范为 UTC、微秒精度的 `...000000Z`，再计算原请求摘要，
不能直接对保留输入 offset 的 wire JSON 计算另一份摘要。
原 command、私有 request_document 和 worker 接受身份仍由原 owner 保存/验证，不能靠投影恢复它们。
查询不回传私有正文。voice 省略/null 沿用原 codec；验证器不填默认值或改写输入。

## receive 与不可逆门

receive 的原子提交是取消线性化点；claim 仅认领后续外部 abort，不能代替 receive。
先核对全身份、原摘要/正文和完整原意图，再查原回执；准确重放先于任何新动作 CAS。
同 submit 的异意图使用现有 `control_state_conflict`，不得覆盖、换键或静默重基。
原回执保留第一次的 intent、digest、received_at、gate_state/version_at_receive；
重放同时返回一致快照中的当前 gate 观察，但不得改写原回执或触碰 gate 时间/version。

| 接收前 gate | 首次 receive 的持久结果 | 后续动作 |
| --- | --- | --- |
| open | 同事务存完整取消原意图，转 cancelled_closed，gate version 加一，记录 DB 时间 | 禁止新 permit/seal；账目不因接收而减少 |
| engine_possible | 存取消原意图，保留 gate state/version/time | 不能逆转为零外发；停止与账目保持原规则 |
| sealed_zero | 存迟到取消原意图，原 seal/terminal/gate version/time 不变 | 不对该终态外发 abort；accepted/recorded 不表示取消获胜 |
| cancelled_closed | 已有相同完整意图回放原记录；缺取消历史为不完整，异意图冲突 | 不补记录、不重新关门或改变时间/version |
| 缺门 | 返回 missing/reconcile 或现有 reconciliation 错误 | 不 INSERT open、不回填、不发送 |

门使用共享 Revision 字符串域，初始值为 `"1"`，不是实验的整数 0。
写者还须检查 revision 的有符号 64 位范围和加一溢出；耗尽停止写入，不回绕。
首次未来 permit 提交只能使 open 永久成为 engine_possible；本候选没有 permit 请求入口。
原 permit 查询/重放不恢复发送权，未知请求不重发，后续每段须另经未来可信边界核验。
sealed_zero 仅为只读历史状态标签，没有成功 seal 输入、成功证据类型或资源释放入口。
文本 epoch、opaque receipt、裸 resources_closed bool 均不能证明资格。

## 独立只读恢复

| status / reason | 解释与动作 |
| --- | --- |
| found / facts_available | 一致快照中的原身份/完整意图和已存在事实可核对，包括空 open；只读观察，不产生执行权限 |
| missing / original_not_found, gate_missing | 缺原提交或门；保留账目、协调，不证明从未外发 |
| incomplete / original_document_missing, original_document_unverifiable, history_inconsistent | 原文/摘要不可验证或历史不闭合；协调，不修补历史 |
| unknown / submit_commit_unknown, object_version_unknown, evidence_unavailable | 只查询原身份；不换键、不重派、不重处理/重 PUT，不 GET latest 猜原版本 |

查询必须用同一个一致数据库快照读取所有相关表；禁止拼接不同提交时点的结果。
数据库适配器应开启 READ ONLY 一致快照事务，使用独立仅 SELECT 的非 owner 角色，
无 DML、DDL、函数副作用、继承或 SECURITY DEFINER 绕写权限。
不得 FOR UPDATE、调用 act(seal)/receive/claim、创建门/回执、改时间/version、访问对象存储或启动处理。
空门、缺门、unknown 和重复查询均须观测前后字节、行数、版本、时间和账目不变。
这仍是 B 的待实现/真实数据库验收条件，本轮 schema 不能证明数据库权限闭包。

写事务仅在已冻结共享锁图后接线：复用现有 parent submit 与唯一键串行首次插入，
缺行 FOR UPDATE 不承担互斥；锁等待后重核全身份、generation/fencing、取消、门/version 和 DB 时间。
不得持 submit 再反取 epoch/head/binding 早序锁。
mutable epoch authority 的最终锁位、binding disable 桥接、上游 Job/Operation 的完整 fan-out、
FK/唯一键隐式等待及 release/record_stop/barrier/runtime 的全锁图仍未冻结，故本轮没有 SQL 候选。

## B 接续与验收边界

B 先用本候选固定接口实现拒绝、receive 和只读查询；真实发送/成功/释放继续关闭。
必须在完整锁图评审后，以同一候选在任务专用 Docker PostgreSQL 上运行两竞争连接及独立观察连接：
receive 已提交且未 claim 时等待中的 permit/seal 锁后拒绝；迟到取消与精确重放保留原事实；
异身份/摘要/意图不写；故障整笔回滚，COMMIT unknown 只查原身份；只读 DB role 的查询无 DML。
另保留 InnoDB、MinIO、旧消费者切换和全部发送入口负控，不能以 schema/fake 代验。

D1 由部署/执行 owner 提供不可绕过的 issuer/verifier、撤销与实际受理边界；
D2 由 B 对接现有 file_asr 确定性 segment request_id，固定逐请求未知/去重；
D3 由 B/资源 owner 提供受限可信 producer 和最后资源覆盖；
D4 由对象 owner/B 提供原 PUT 意图、固定 version/verified 回执的持久协议。
缺这些只阻塞对应正向接线，不阻塞本文的拒绝/查询核心。

处理完成必须与最终资源关闭分开：处理产物可先固定，但随后的最后 PUT、固定版本 GET、
校验、缓冲和全部子任务 join 仍会占用资源。最终关闭事实必须覆盖这些后续增量，
不能把 PUT 前快照当作整条执行已关闭，更不能当作整实例停止或 baseline 可释放。
本候选未定义可成功消费的上述事实，仍沿现有停止/释放语义处理。

最终交付包保存 patch/完整新文件、manifest/hash、固定输入和原始验证记录。
下一接收人为唯一 SM+PO，供其安排非作者 Sol 审查及 B 接续；本作者不自签高风险审查，
不声称 B 已接入。没有 commit、push、GitHub 写入、共享数据读取或运行服务变更。
