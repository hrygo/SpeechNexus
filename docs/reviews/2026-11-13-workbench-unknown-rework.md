# 工作台失败处置三分类返修的定点复核（PR #692，head 29c13c7e）

> 过程记录（返修复核）。复核对象：`8d858e68..29c13c7e2ed2b68dda10b3d9fdf2caea1aaca8d2`，只覆盖上一轮四项必须修与新引入的 `operation` 输入面，不重跑全量审查。实际复核时间 2026-11-14 04:10–04:22（本地）；文件名沿用首轮约定。
> 环境：独立 worktree `/private/tmp/sn695-r2`（分支 `review/695-rework`，based 29c13c7e）；变异只在 `/private/tmp/sn695-r2-mut`（未入库）。

## 结论：可以合并

四项必须修全部落实，13 failed | 6 passed 的变异结果与 770 全绿均独立复现，未发现新缺陷。仅留两处非阻塞措辞/耦合注记（§1 nit、§2 残留耦合）。

## 四项逐条

### 1. 注释事实错误 —— 成立

读了 `file-task-session.ts:35-135` 原文（不是只看 diff）：

- 旧的「4xx 拒绝证明该 key 什么都没写」「协议声明无 Job 行」两处全称判断已删除；新文本明确写「terminal 只 settle 本次尝试，不断言 key 没有 Job 行」，并给出两个反例：`409 idempotency_conflict` 是同一 key 下另一意图的 Job（`contracts/speech-openapi.yaml:803-805`），`409 execution_cleanup_pending` 证明存在终态 Job（`:2852-2854`），创建路由的通用 409 组件也列了 cleanup pending（`:2860-2865`）。
- 行号已改为 `job_errors.py:129-134`；`job_unavailable` 的 `retryable: false` 引 `:113`，协议措辞引 `ServiceUnavailable` `:2897-2901`；`intent-kept` 改名与「client 完全不消费 `error.retryable`」的说明一致（我复核过 `file-workflow` 无消费点）。
- 我在原文里挑的一处残留 nit：`Any other 4xx contract refusal … the protocol does not accept the same request again` 对 `408 asset_receive_timeout` 不精确（408 是超时不是 refusal，同一请求稍后重试可能成功）；但处置仍正确（契约保证 `expired before reservation`，无写入）。不阻塞。

### 2. `409 execution_cleanup_pending` 的操作上下文 —— (a) 成立；(b) 未发现可达错标

(a) 独立 probe 实测：

```text
create 409 execution_cleanup_pending → unknown=true,  canReplay=true,  recovery=该码, canFinishTask=false
cancel 409 execution_cleanup_pending → unknown=false, canReplay=false, recovery=undefined
upload 409 execution_cleanup_pending → unknown=true,  canReplay=true （无 record，推导为 upload）
```

作者的两个 session 用例（create 保 unknown / cancel 置 terminal）也在 770 全绿中。restored `pending:"cancel"` 的重放走 cancel 上下文 → 409 正确清掉复活的 unknown。

(b) 推导来源是持久意图，但**每个 mutation 动作在发请求前先落自己的标记**：`#submit` 在 `client.create` 前 `#persist({pending:"create"})`（`:671`），`#cancelIntent` 在 `client.cancel` 前 `#persist({pending:"cancel"})`（`:807`）；`#persist` 抛错时是 `TaskJournalError`（非 `SpeechResponseError`），根本不进入分类。因此 catch 时推导出的 operation 与在飞请求一致，即使未来有路径在 stale `pending:"cancel"` 记录上调 `#submit`，`#submit` 也会先把 pending 改成 `"create"` 再发请求。实测：terminal cancel 之后 `continueAfterValidation`/`replay`/`start` 全部 no-op，`createKeys` 不增；没有可达路径把 create 标成 cancel（唯一危险方向）。

残留耦合（不阻塞）：operation 是从记录状态推导而非动作自身携带；若未来新增一个 mutation 类且忘了先落 pending 标记，会默认落 `"create"`（保守方向，create 的 cleanup 规则保 unknown），不会误判成 cancel。

### 3. `recovery` 保留与「durable/可见指向同一执行」—— 成立

- `retainRecovery` 三例的预期与实现一致：已识别者优先（即使 incoming 带另一个身份）、带 `nexus.operation_id` 者优先于无、都无则保留首个 envelope。
- adopt 场景实测：`503 job_outcome_unknown` 带 `op-demo` 后，可见 `recovery.error.nexus.operation_id === "op-demo"`，journal 里 `operationId === "op-demo"`，同一值；keep 场景两者均为 `op-first`。
- 旧错配（可见 op-first、durable op-second）现在结构上不可能：`#persist({operationId})` 只在 `recovery === contractError.detail`（即采用 incoming 身份）时发生；保留 previous 时不写盘，durable 保持原值。
- 注：旧错配在旧代码的公共 API 下本来也不可达（一旦有身份则 `canReplay=false` 且 `unknown=true`，所有提交入口被挡），所以这是防御性修复而非可达 bug 修复；`#persist` 失败时会留下「durable 为空 + 可见有身份」，不是两个不同执行。

### 4. 回滚必红 —— 成立

- 恒 `terminal` 变异：`Tests 13 failed | 6 passed (19)`。6 绿 = 2 个旧 terminal 用例 + cancel cleanup 用例 + `retainRecovery` 3 例；13 红 = 上一版 10 + 新增 create cleanup、keep earlier recovery、adopt identity。
- 单独短路操作上下文规则（`if (false && operation !== "cancel" && …)`）：create 409 用例红在 `:283 expect(snapshot).toMatchObject({ unknown: true, canReplay: true })`，实际收到 `{ canReplay: false, unknown: false }`（意图被清）；同变异下 cancel 用例仍绿（terminal 不变）。正是要区分的方向。

## 新绿（通过但证明力弱）

1. adopt 身份用例里的 `503 + nexus.operation_id` 仍是合成组合：后端 `job_errors.py` 构造错误体时不附 `nexus`（`nexus` 只用于 `operation_in_progress`/`response_not_replayable`，当前后端也未发出），该持久化路径只有合成覆盖。
2. `upload` 的操作上下文无生产用例且不可达：Asset 路由不会返回 `execution_cleanup_pending`；且 `"upload"` 与 `"create"` 当前行为完全相同（只有 `"cancel"` 触发特判），三值类型实际只做二分判别。
3. 次要：`retainRecovery` 仅为测试导出；作者没有直接单测 `(status, code, operation)` 矩阵（create/cancel 两条经 session 覆盖，够用）。

未发现新红。

## 定点门禁

- head 29c13c7e：`62 files / 770 tests` 全绿（8d858e68 为 763，+7，与 19−12 一致）。
- `web/workbench`：`npx tsc -p tsconfig.json` 无诊断。
- `uv run --no-project ruff format --check .` → `1814 files already formatted`；`git status` 干净，`uv.lock` 未变。
- 独立 probe 20 例全绿：纯函数 operation 矩阵 12 组；session 5 例（create/cancel/upload 三上下文、restored cancel 重放、terminal cancel 后无 create 泄漏）；durable/可见身份一致 2 例与 `retainRecovery` 混合规则 1 例。
