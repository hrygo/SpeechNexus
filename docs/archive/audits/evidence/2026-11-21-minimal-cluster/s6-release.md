# 最小同构集群 S6：租约与预算按停止证据释放

- 采集时间：2026-11-21 01:30–01:34（Asia/Shanghai）
- 结论：**成功 Job 的租约与预算已按停止证据自动归还，S6 通过。**
  首个 Job 的归还是手工补做的，第二个 Job 起为派发器自动完成。

## 首个 Job：自动归还没有发生

Job `job-a45968b4…` 成功后，`execution_stop_evidence` 已有 1 行、意图为 `succeeded/complete`，
但派发器只打 `lease release deferred to reconciliation`，计数仍 `held_slots=1`、
`held_attempts=1`、租约 `reserved/pending`。

用派发器自己的 `LeaseService.release` 复核同一条租约：

```
lease lease-a45968b4cbbd43d99f04016b516d12d4 version 2
outcome replayed  state released  cleanup complete  error None
```

即释放本身成立，失败在超时：派发器的存储预算是 3 s（`SPEECHNEXUS_JOB_DISPATCH_STORAGE_TIMEOUT_SECONDS`），
隧道化 PG 上这次释放超过了它；一次失败后不再重试，直到对账。
与网关控制事务 3 s 超时是同一类问题。

## 第二个 Job：自动释放成立

把派发器的存储预算提到 10 s 后重建 `dispatch`，跑第二个 Job：

| 观测点 | 结果 |
| --- | --- |
| Job | `job-a45968b4…` 之后的新 Job，`queued → running → succeeded` |
| 制品 | `{"text":"The quick brown fox jumps over the lazy dog.","duration_ms":3447}` |
| 制品 sha256 | 记录与下载重算一致：`da4d6eb2acf468fc84a98323f15007317f9cc1ba76193dc84be4948e404e0c53` |
| `engine_instances.held_slots` | `0` |
| `workspace_quotas.held_attempts` | `0` |
| `admission_leases` | `lease_state=released`、`cleanup_state=complete`、`budget_released_at_us` 非空 |
| 派发器日志 | 无 `lease release deferred to reconciliation` |

## 结论口径

- “按停止证据释放”指 `execution_stop_evidence` 与意图 `succeeded/complete` 是释放的前置事实，
  释放事务读取它们；不是“Job 结束即归还”。
- 首个 Job 的手工释放**不算**自动路径的证据；自动路径由第二个 Job 证明。
- 5 s/3 s 这类默认预算在隧道化数据库上普遍偏紧，算力面与派发器都需要显式放大。
