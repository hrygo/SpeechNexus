# 团队 C 工作台操作覆盖映射（workbench surface）

核验日期：2026-10-26，Asia/Shanghai。基线：main `e2fee58799f2`，契约 `contracts/workbench-openapi.yaml`（digest `4aed45a36837…`）。关联：[阶段 Goal](../plans/2026-10-25-team-c-next-phase-goal-and-acceptance.md)、[映射 JSON](2026-10-26-team-c-operation-coverage.json)。

## 结论

workbench surface 的 51 个声明操作已逐项建立“契约 → 绑定与边界类型 → 生产消费者 → 行为用例”映射：39 个四条腿齐全（`bound`），12 个缺生产消费者（`consumer_pending`）。12 个非 `bound` 操作均已具名登记 owner、制品、解除条件和影响，不再以“保留声明”冒充覆盖。**本映射是接线审计，不是 AC-03 通过或任何验收结算**；其他 surface（admin/profile/binding 等）另行核验。

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
| `generateRenderUnit` | consumer_pending | 有声书生成触发与单元轮询的正式页面消费者（C07/T09） | 页面只能建/改单元与读取音色修订，无法发起真实生成或读取 v2 生成结果；C07/T09 未通过 |
| `generateRenderUnitV2` | consumer_pending | RenderUnitPanel/BooksHost 的生成触发、v2 轮询与未知结果恢复（C07/T09） | 后端可提交并记录固定版本生成，但页面仍不能发起或读取生成；真实 speech 投递与生成终态回写未验证；C07/T09 未通过 |
| `getChapter` | consumer_pending | 单章读取/单修订读取的使用决定或退役（C07/C02） | 无生产消费者；当前页面行为不受影响，但声明操作缺处置，AC-03 未通过 |
| `getRenderUnit` | consumer_pending | v1/v2 兼容与退役决定（C00/AC1 契约维护） | 正式页面已全部使用 v2，v1 无生产消费者；AC-03 要求每个声明操作有具名处置，不以保留声明冒充覆盖 |
| `getRenderUnitV2` | consumer_pending | 有声书生成触发与单元轮询的正式页面消费者（C07/T09） | 页面只能建/改单元与读取音色修订，无法发起真实生成或读取 v2 生成结果；C07/T09 未通过 |
| `getTranscriptRevision` | consumer_pending | 单章读取/单修订读取的使用决定或退役（C07/C02） | 无生产消费者；当前页面行为不受影响，但声明操作缺处置，AC-03 未通过 |
| `getVoiceRevision` | consumer_pending | v1/v2 兼容与退役决定（C00/AC1 契约维护） | 正式页面已全部使用 v2，v1 无生产消费者；AC-03 要求每个声明操作有具名处置，不以保留声明冒充覆盖 |
| `getWorkbenchStatus` | consumer_pending | v1/v2 兼容与退役决定（C00/AC1 契约维护） | 正式页面已全部使用 v2，v1 无生产消费者；AC-03 要求每个声明操作有具名处置，不以保留声明冒充覆盖 |
| `listRenderUnits` | consumer_pending | v1/v2 兼容与退役决定（C00/AC1 契约维护） | 正式页面已全部使用 v2，v1 无生产消费者；AC-03 要求每个声明操作有具名处置，不以保留声明冒充覆盖 |
| `streamExportEvents` | consumer_pending | 导出进度 SSE 正式消费者（T09/AC-08） | 导出进度仅靠轮询 getBookExport；SSE 的续读/410/去重语义无正式消费者，AC-08 未通过 |
| `updateRenderUnit` | consumer_pending | v1/v2 兼容与退役决定（C00/AC1 契约维护） | 正式页面已全部使用 v2，v1 无生产消费者；AC-03 要求每个声明操作有具名处置，不以保留声明冒充覆盖 |

## 限制与下一步

- 只覆盖 workbench surface；`admin`、`profile`、`profile_lifecycle`、`binding`、`binding_defaults`、`voice_catalogue` 等 surface 仍需同类映射。
- 消费者识别基于路径/方法文本与客户端方法解析，可能漏掉动态拼接路径或间接调用；不据此宣称“已覆盖”任何行为。
- 下一单元：按 [C07/T09 缺口](../plans/2026-10-25-team-c-next-phase-goal-and-acceptance.md) 接通生成触发、v2 单元轮询与导出 SSE 的正式消费者；v1 兼容操作由契约 owner 给出保留/退役决定。
