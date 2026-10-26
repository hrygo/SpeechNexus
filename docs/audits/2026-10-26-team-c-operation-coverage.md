# 团队 C 工作台操作覆盖映射（workbench surface）

核验日期：2026-10-26，Asia/Shanghai。机器映射受检 head：`15173b1aba73a19c1079e72e96d870043394554b`；收尾核对 main `54daef6ae4792b69a0e5e8645b09240ff715082e` 的契约摘要仍为 `e0138dff9de096a5fc8d2c10c5c7ba532df6736a99091d4ef601d144a20438c9`。关联：[阶段 Goal](../plans/2026-10-25-team-c-next-phase-goal-and-acceptance.md)、[映射 JSON](2026-10-26-team-c-operation-coverage.json)。

## 结论

workbench surface 的 52 个声明操作已逐项建立“契约 → 绑定与边界类型 → 生产消费者 → 行为用例”映射：42 个四条腿齐全（`bound`），10 个缺生产消费者（`consumer_pending`）。10 个非 `bound` 操作均已具名登记 owner、制品、解除条件和影响，不再以“保留声明”冒充覆盖。**本映射是接线审计，不是 AC-03 通过或任何验收结算**；其他 surface（admin/profile/binding 等）另行核验。

## 判定口径

- **绑定**：操作路径出现在 `contracts/bindings.json` 的 workbench `paths`；操作名出现在生成的 `packages/contracts/src/workbench-http.ts` `HttpOperation` 联合与 `speechnexus_contracts/workbench-http.schema.json` 的 `x-nexus-http-operations`。
- **生产消费者**：`web/workbench/src` 的非测试、非生成文件出现该操作的 `METHOD("path")` 调用，或 `validateRequest/Response/Event("<operation>")`。
- **行为用例**：`web/workbench/src` 测试或 `tests/` 中出现操作名、`METHOD("path")`、客户端对应方法调用（`client.method(...)`），或以字符串指定该客户端方法（`vi.spyOn(client, "method")`）。客户端方法从 `workbench-client.ts` 的 `async <method>` 与其 `#api.METHOD("<path>")` 解析。
- 文本或路径命中只证明接线存在，不证明行为正确；具体正负例、恢复语义与真实服务资格仍按原 AC 逐项核验。

## 门禁

```bash
uv run python scripts/dev-env/check-operation-coverage.py            # 审计，漂移返回非零
uv run python scripts/dev-env/check-operation-coverage.py --write    # 重新生成并保留已登记 pending
make operation-coverage-check
```

`tests/unit/test_operation_coverage.py` 15 项反例分别确认：声明操作缺映射、多余映射、删消费者、删用例、绑定标记漂移、消费者出现后登记过期、无消费者却标 bound、登记缺字段、契约摘要变化均会使门禁失败；并核对当前提交的映射与派生结果一致。用例识别包含 spy 目标方法名，避免把已存在的行为测试误判为缺失（首版曾因此虚报 3 项缺用例，已修正）。

## 非 bound 操作登记

| 操作 | 状态 | 缺什么（制品） | 影响 |
| --- | --- | --- | --- |
| `createRenderUnit` | consumer_pending | v1/v2 兼容与退役决定（C00/AC1 契约维护） | 正式页面已全部使用 v2，v1 无生产消费者；AC-03 要求每个声明操作有具名处置，不以保留声明冒充覆盖 |
| `generateRenderUnit` | consumer_pending | v1 生成操作保留/退役决定（v2 生产路由与页面消费者已就绪） | v1 生成无路由与消费者；页面只使用 v2。C07/T09 未通过，真实 speech 投递仍未验证 |
| `getChapter` | consumer_pending | 单章读取/单修订读取的使用决定或退役（C07/C02） | 无生产消费者；当前页面行为不受影响，但声明操作缺处置，AC-03 未通过 |
| `getRenderUnit` | consumer_pending | v1/v2 兼容与退役决定（C00/AC1 契约维护） | 正式页面已全部使用 v2，v1 无生产消费者；AC-03 要求每个声明操作有具名处置，不以保留声明冒充覆盖 |
| `getTranscriptRevision` | consumer_pending | 单章读取/单修订读取的使用决定或退役（C07/C02） | 无生产消费者；当前页面行为不受影响，但声明操作缺处置，AC-03 未通过 |
| `getVoiceRevision` | consumer_pending | v1/v2 兼容与退役决定（C00/AC1 契约维护） | 正式页面已全部使用 v2，v1 无生产消费者；AC-03 要求每个声明操作有具名处置，不以保留声明冒充覆盖 |
| `getWorkbenchStatus` | consumer_pending | v1/v2 兼容与退役决定（C00/AC1 契约维护） | 正式页面已全部使用 v2，v1 无生产消费者；AC-03 要求每个声明操作有具名处置，不以保留声明冒充覆盖 |
| `listRenderUnits` | consumer_pending | v1/v2 兼容与退役决定（C00/AC1 契约维护） | 正式页面已全部使用 v2，v1 无生产消费者；AC-03 要求每个声明操作有具名处置，不以保留声明冒充覆盖 |
| `streamExportEvents` | consumer_pending | 导出进度 SSE 正式消费者（T09/AC-08） | 导出进度仅靠轮询 getBookExport；SSE 的续读/410/去重语义无正式消费者，AC-08 未通过 |
| `updateRenderUnit` | consumer_pending | v1/v2 兼容与退役决定（C00/AC1 契约维护） | 正式页面已全部使用 v2，v1 无生产消费者；AC-03 要求每个声明操作有具名处置，不以保留声明冒充覆盖 |

## 限制与下一步

- 只覆盖 workbench surface；`admin`、`profile`、`profile_lifecycle`、`binding`、`binding_defaults`、`voice_catalogue` 等 surface 仍需同类映射。
- 消费者识别基于路径/方法文本与客户端方法解析，可能漏掉动态拼接路径或间接调用；不据此宣称“已覆盖”任何行为。
- 生成 V2、单元轮询和历史 GET/页面已有分段消费者，导出 SSE 仍待；未决持久化/CAS、读取与公平性审查见[最终交接](../plans/2026-10-26-team-c-final-handoff.md)。v1 兼容操作由契约 owner 决定保留/退役，新负责人安排后续实施。
