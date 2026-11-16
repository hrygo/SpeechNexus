# 提案：「执行已 `succeeded` 而后到的取消」如何收口

状态：**待裁决，本切片不实施**。本文只记录竞争方案与取舍，供决定是否另开实施切片。
相关：[`docs/components/production-jobs.md`](../components/production-jobs.md) 的「取消失败的终态判定」、
`#852`、`#338`。

## 事实（`#852` 实测，2026-11-16）

取消在「执行已到达成功终态、但没有停止证据」时无法收口：

| 观察项 | 实际值 |
|---|---|
| `speech_jobs.state` | 恒为 `cancelling`（判定分支拒绝推进） |
| `speech_jobs.error_code` | `NULL` |
| `admission_leases.lease_state` | 恒为 `reserved` |
| `engine_instances.held_slots` | 不归零（容量一直算被持有） |
| 账本 `attempts` | 涨到 64 后由 `LEAST(attempts+1, 64)` 饱和，之后按最大退避继续领取 |
| `cancelling → succeeded` | 数据库状态机触发器拒绝，不存在「自动变成成功」的分支 |

根因是两条规则同时成立，而它们各自都对：

1. 隔离（`LeaseService.quarantine`）只接受执行事实为 `unknown_quarantined`/`quarantined` 的租约，
   已完成（`succeeded`）的执行不在其中——不能把一个「已经跑完、只是没人确认资源已回收」的执行
   记成「未知隔离」。
2. 释放（`LeaseService.release`）要求提交 `cancelled` 的停止与资源释放双证据；`succeeded`
   本身不是停止证据，也不是本进程发出的释放证据。

于是没有任何一方能把 Job 或租约推到确定态，而 `production-jobs.md` 曾写「不能停在
`cancelling`」——那句话在当前实现下不成立，本次已改成事实表述。

## 候选方案

### A. 完成方赢下竞争（需要改触发器与状态机）

`cancelling` 期间若执行以 `succeeded` 收尾且其资源释放证据齐全，则由完成路径把 Job 推进到
`succeeded`，取消受理记为「到达太晚」并结算。

- 优点：符合「谁先成立谁赢」的直觉；容量随完成路径正常归还，不需要新的操作员动作。
- 代价：要改公开状态机（允许 `cancelling → succeeded`）、要改数据库触发器、读模型
  `cleanup_state`/`retryable` 的输入集合要重算，还要定义「完成证据齐全」的判定标准
  （现在完成路径并不携带停止/释放证据，那属于 #338 的协议）。
- 风险：`cancelling` 之后出现 `succeeded`，会让「取消已受理」这个对外承诺变成「可能最终成功」，
  与 `X-GATEWAY` 里「取消受理不是停止证据」的边界相邻，容易被误读为「取消可被完成覆盖」。

### B. 有界操作员路径（不改状态机）

保持状态机不变，给这类受理一条显式的操作员出口：在可读面上点名「执行已完成但无释放证据」，
由操作员对固定 instance/generation 执行 drain，完成 `operator_verified_instance_terminated`
证词后走既有 `QuarantineReleaseService` 释放租约；Job 仍以 `failed/cancellation_unconfirmed`
收口。

- 优点：不动状态机、不动触发器、不新增语义；复用既有证词与释放路径。
- 代价：依赖人工；需要把「如何识别这一类」写进 runbook，并给出警示面（与 `cancellation_debt`
  同一前缀下的一个子类）。
- 风险：`operator_verified_instance_terminated` 的语义是「已验证实例终止」，对一个已经成功
  完成的执行是否成立，需要领域负责人确认；否则就是拿证词套一个不匹配的事实。

### C. 扩展隔离的事实白名单（最小改动，但语义最重）

允许以「执行已完成 + 释放未确认」为理由隔离租约（例如 `reason_code=cancellation_unconfirmed`，
事实取 `succeeded`），把容量移入隔离桶，Job 照现有规则落 `failed/cancellation_unconfirmed`。

- 优点：改动最小，账目立即自洽（`held_slots` 归零、`quarantined_slots` 增加）。
- 代价：隔离的语义从「执行未知」扩展到「执行已完成」，需要领域层显式区分这两类隔离，
  并且释放路径必须能区分「隔离的未知执行」与「隔离的已完成执行」。
- 风险：一旦隔离可以覆盖「已完成」，未来任何「执行状态读不到/读错」的实现缺陷都会更容易
  伪装成「已隔离」，这正是 #852 要堵的那类谎报。

### D. 保持现状 + 只把可见面写实（本切片已做的部分）

不做收口，实测在文档与发布说明里写明：这类受理会停在 `cancelling`，租约 `reserved`，
`attempts` 饱和在 64，需要 #338 或操作员处置。

- 优点：零语义风险，本切片可直接合入。
- 代价：容量一直计入 `held_slots`；`/readyz` 的 detail 会长期报到这一类。

## 建议

先做 **D（已完成）**，把 **B** 作为下一格的候选，**A** 与 **C** 需要领域与安全两侧的裁决：

- 选 B 的条件：确认 `operator_verified_instance_terminated` 能覆盖「执行已成功完成」的实例，
  或新增一条等价的、不降低证据强度的证词。
- 选 A 的条件：#338 落地后完成路径能携带可证明的停止/释放证据，且产品接受
  「取消受理后仍可能成功」的对外语义。
- 选 C 的条件：隔离的事实白名单由领域层显式建模成两类，且释放路径按类型分别校验；否则不建议。

## 未决问题（裁决时需要回答）

1. 完成方（`succeeded`）与取消方谁优先？现有 `X-GATEWAY` 只规定「取消受理不是停止证据」，
   没有规定终止状态的优先级。
2. 一个已 `succeeded` 的执行，其容量归还的依据是什么：完成路径的释放证据，还是操作员证词？
3. 若选 A，读模型 `cleanup_state` 在 `cancelling` 期间要不要引入「可能成功」的新值？
   这会扩公开契约，需单独审批。

## 裁决与落地（2026-11-16，方案 B）

选定 **B**：保留状态机与触发器，不放宽
`quarantine_release.require_releasable`，只给这一类受理一条显式、有界的操作员出口。
落地方式与本文最初设想的差异，记录如下：

- **不新增路由**：`QuarantineReleaseRequest` 的冻结正文只有
  `{evidence_kind, evidence_digest, observed_at, reason}`，后端无法在其中核对 drain 事实，
  钉住 instance/generation 会退化成「操作员说了算」。出口因此是实现侧的操作员命令
  `python -m speechnexus_backend.completed_release_cli`，公开契约与 `contracts/**` 不变。
- **证词分离**：不复用未知执行的「实例已终止」证词，新增
  `completed-release-evidence/v1` 摘要（`lease_id` + `submit_intent_id` +
  `instance_id` + `instance_generation` + 整份 drain 应答 + `observed_at` + `reason`），
  并在写事务内按加锁事实重算比对。同一账本、同一释放路径（`LeaseService.release`），
  但两条证词不通用。
- **drain 是前提而非建议**：只有 `drain_state='drained'`、
  `safe_to_stop_instance=true` 且三类未完成计数全 0 才放行；「执行已完成但实例未 drain」
  与「已 drain」绝不共用一句证词。
- **判据**：租约 `reserved` + 执行 `succeeded`/`cleanup_state=complete` + 网关
  `engine_terminal_response` 证据（`execution_stopped=1`、`resources_released=1`）。
  缺该证据的仍属 #338，不提供出口。
- 可见面上，`cancellation_debt` 前缀下新增子类
  `completed-without-release`（与既有的 `execution-unknown` 并列且分别计数），
  处置顺序写在[生产作业组件文档](../components/production-jobs.md)的既有处置段。

未决问题 1、3 仍然有效（谁优先、`cleanup_state` 是否扩值）；问题 2 的答案已由本方案给出：
归还依据是「网关终止证据（执行确实结束）+ 操作员对该实例代次的 drain 证词（实例确实干净）」，
两者都在加锁事务内被重新核对，操作员单独一句话不能释放。
